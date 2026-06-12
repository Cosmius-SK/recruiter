"""The recruitment-lifecycle orchestration graph.

Design principles
-----------------
- **Agents act, the graph governs.**  Each node either runs one autonomous
  specialist (which decides its own tool usage) or enforces a policy gate.
- **Humans decide, never push paper.**  Every ``interrupt()`` is a genuine
  judgement call: JD sign-off, finance exceptions, flagged profiles, the hire
  decision, out-of-band offers, BGV adjudication.  Everything else is
  automated.
- **Durable by construction.**  The graph is checkpointed; an interrupt parks
  the workflow (for minutes or weeks — e.g. while interviews happen) and a
  ``Command(resume=...)`` wakes it exactly where it stopped.
- **Trust but verify.**  After an agent runs, the node verifies the contract
  against the systems of record (calendar, BGV vendor, ITSM) and backfills
  deterministically if the agent missed a mandatory action.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, Send, interrupt

from talentflow.agents.specialists import (
    consolidate_feedback,
    draft_requisition,
    negotiate_offer,
    parse_brief,
    prescreen_candidate,
    run_onboarding,
    run_sourcing,
    schedule_interviews,
    screen_candidate,
    supervisor_review,
)
from talentflow.config import get_settings
from talentflow.domain.models import (
    AuditEvent,
    BGVStatus,
    Offer,
    OfferStatus,
    OnboardingPlan,
    Requisition,
    RequisitionStatus,
    Scorecard,
    agent_event,
    human_event,
    system_event,
)
from talentflow.integrations.store import get_store
from talentflow.orchestrator import hitl
from talentflow.orchestrator.state import RecruitmentState, ScreenTask


def _trace_events(stage: str, report, cap: int = 30) -> list[AuditEvent]:
    return [AuditEvent(stage=stage, actor="agent", action="tool_call", detail=line)
            for line in report.tool_trace[:cap]]


# ---------------------------------------------------------------------------
# Stage 0 — intake
# ---------------------------------------------------------------------------

def intake_node(state: RecruitmentState) -> dict:
    role = parse_brief(state["brief"])
    return {
        "role_spec": role,
        "jd_revisions": 0,
        "sourcing_rounds": 0,
        "events": [agent_event("intake", "Parsed hiring brief",
                               f"{role.title} ({role.level}), {role.department}, {role.location}")],
    }


# ---------------------------------------------------------------------------
# Stage 1 — requisition (budget gate + JD drafting)
# ---------------------------------------------------------------------------

def requisition_node(state: RecruitmentState) -> Command[Literal["jd_approval", "wrapup"]]:
    store = get_store()
    role = state["role_spec"]
    band = store.get_comp_band(role.level) or store.get_comp_band("L5")

    budget_ok, budget_detail = store.check_budget(role.department, role.headcount)
    events = [system_event("requisition", "HRIS budget check", budget_detail)]
    if not budget_ok:
        return Command(goto="wrapup", update={
            "outcome": "closed_lost",
            "outcome_summary": f"Requisition blocked at budget check: {budget_detail}",
            "events": events,
        })

    draft, report = draft_requisition(role, state.get("jd_feedback"))
    events += _trace_events("requisition", report)

    existing = state.get("requisition")
    req_id = existing.id if existing else f"REQ-{uuid.uuid4().hex[:6].upper()}"
    proposed = draft.proposed_base_max or band.base_mid
    requisition = Requisition(
        id=req_id, role=role, comp_band=band,
        jd_markdown=draft.jd_markdown, proposed_base_max=proposed,
        approved_base_max=min(proposed, band.base_max),
        budget_ok=True, budget_summary=draft.budget_summary or budget_detail,
        status=RequisitionStatus.PENDING_APPROVAL,
    )
    events.append(agent_event("requisition", "JD drafted",
                              f"{req_id}: proposed base ceiling ${proposed:,} "
                              f"(band max ${band.base_max:,})"))
    return Command(goto="jd_approval", update={"requisition": requisition, "events": events})


def jd_approval_node(state: RecruitmentState) -> Command[
    Literal["requisition", "finance_approval", "sourcing", "wrapup"]
]:
    settings = get_settings()
    answer = interrupt(hitl.jd_approval_payload(state))
    req = state["requisition"]

    if not answer.get("approved", False):
        revisions = state.get("jd_revisions", 0) + 1
        feedback = answer.get("feedback", "Please revise the draft.")
        if revisions > settings.max_jd_revisions:
            return Command(goto="wrapup", update={
                "outcome": "closed_lost",
                "outcome_summary": f"JD failed approval after {revisions} revision cycles.",
                "events": [human_event("jd_approval", "JD rejected — revision limit reached", feedback)],
            })
        return Command(goto="requisition", update={
            "jd_feedback": feedback,
            "jd_revisions": revisions,
            "events": [human_event("jd_approval", "JD revision requested", feedback)],
        })

    approved = req.model_copy(update={"status": RequisitionStatus.OPEN})
    events = [human_event("jd_approval", "JD approved", f"by {req.role.hiring_manager}")]
    if req.proposed_base_max > req.comp_band.base_max:
        return Command(goto="finance_approval", update={"requisition": approved, "events": events})
    ceiling = req.proposed_base_max or req.comp_band.base_max
    return Command(goto="sourcing", update={
        "requisition": approved.model_copy(update={"approved_base_max": ceiling}),
        "offer_ceiling": ceiling,
        "events": events,
    })


def finance_approval_node(state: RecruitmentState) -> Command[Literal["sourcing"]]:
    answer = interrupt(hitl.finance_approval_payload(state))
    req = state["requisition"]
    if answer.get("approved", False):
        ceiling = int(answer.get("approved_max") or req.proposed_base_max)
        event = human_event("finance_approval", "Comp exception approved",
                            f"ceiling raised to ${ceiling:,}")
    else:
        ceiling = req.comp_band.base_max
        event = human_event("finance_approval", "Comp exception rejected",
                            f"proceeding within band max ${ceiling:,}")
    return Command(goto="sourcing", update={
        "requisition": req.model_copy(update={"approved_base_max": ceiling}),
        "offer_ceiling": ceiling,
        "events": [event],
    })


# ---------------------------------------------------------------------------
# Stage 2 — sourcing + parallel screening fan-out
# ---------------------------------------------------------------------------

def sourcing_node(state: RecruitmentState) -> dict:
    store = get_store()
    req = state["requisition"]
    round_no = state.get("sourcing_rounds", 0) + 1

    report = run_sourcing(req, round_no)
    events = _trace_events("sourcing", report)

    # Contract: the candidate pool comes from the systems of record (ATS inbound
    # + talent-pool matches), regardless of how the agent phrased its outreach.
    existing = {c.id for c in state.get("candidates", [])}
    pool = store.fetch_inbound_applications() + store.search_talent_pool(req.role.must_have_skills)
    new_candidates, seen = [], set(existing)
    for cand in pool:
        if cand.id not in seen:
            seen.add(cand.id)
            new_candidates.append(cand)

    events.append(agent_event("sourcing", f"Sourcing round {round_no} complete",
                              f"{len(new_candidates)} new candidate(s) in the funnel"))
    return {
        "candidates": state.get("candidates", []) + new_candidates,
        "sourcing_rounds": round_no,
        "events": events,
    }


def dispatch_screening(state: RecruitmentState):
    """Fan one screening branch out per unscreened candidate (map-reduce)."""
    screened = {s.candidate_id for s in state.get("screening", [])}
    todo = [c for c in state.get("candidates", []) if c.id not in screened]
    if not todo:
        return "shortlist_gate"
    return [Send("screen", ScreenTask(requisition=state["requisition"], candidate=c)) for c in todo]


def screen_node(task: ScreenTask) -> dict:
    result = screen_candidate(task["requisition"], task["candidate"])
    return {
        "screening": [result],
        "events": [agent_event("screening", f"Screened {task['candidate'].name}",
                               f"score {result.score}/100 — {result.recommendation}")],
    }


def shortlist_gate_node(state: RecruitmentState) -> Command[
    Literal["sourcing", "shortlist_review", "prescreen", "wrapup"]
]:
    settings = get_settings()
    screening = state.get("screening", [])
    advanced = sorted((s for s in screening if s.recommendation == "advance"
                       or (s.recommendation != "flag_for_human"
                           and s.score >= settings.screening_advance_threshold)),
                      key=lambda s: -s.score)
    flagged = [s for s in screening if s.recommendation == "flag_for_human"]
    auto_ids = [s.candidate_id for s in advanced][: settings.shortlist_size]
    can_expand = state.get("sourcing_rounds", 0) < settings.max_sourcing_rounds

    events: list[AuditEvent] = [system_event(
        "shortlist", "Shortlist policy evaluated",
        f"{len(advanced)} qualified, {len(flagged)} flagged, top {len(auto_ids)} auto-shortlisted")]

    if not auto_ids and not flagged:
        if can_expand:
            events.append(system_event("shortlist", "No qualified candidates — expanding sourcing"))
            return Command(goto="sourcing", update={"events": events})
        return Command(goto="wrapup", update={
            "outcome": "closed_lost",
            "outcome_summary": "No qualified candidates after exhausting sourcing rounds.",
            "events": events,
        })

    if len(advanced) < settings.min_qualified_for_shortlist and can_expand:
        # Borderline pool: let the LLM supervisor weigh slate quality vs. delay.
        decision = supervisor_review(
            f"The pipeline has {len(advanced)} qualified candidate(s) (policy prefers at least "
            f"{settings.min_qualified_for_shortlist}) plus {len(flagged)} flagged profile(s) "
            f"after sourcing round {state.get('sourcing_rounds', 0)} of "
            f"{settings.max_sourcing_rounds}. Top scores: "
            f"{[s.score for s in advanced] or 'none'}. Proceed with the current slate, or run "
            f"another sourcing round?")
        events.append(agent_event("supervisor", f"Supervisor decision: {decision.action}",
                                  decision.rationale))
        if decision.action == "expand_sourcing":
            return Command(goto="sourcing", update={"events": events})

    if flagged:
        return Command(goto="shortlist_review", update={"shortlist": auto_ids, "events": events})
    return Command(goto="prescreen", update={"shortlist": auto_ids, "events": events})


def shortlist_review_node(state: RecruitmentState) -> Command[Literal["prescreen"]]:
    flagged_ids = [s.candidate_id for s in state.get("screening", [])
                   if s.recommendation == "flag_for_human"]
    auto_ids = state.get("shortlist", [])
    answer = interrupt(hitl.shortlist_review_payload(state, auto_ids, flagged_ids))
    included = [cid for cid in answer.get("include_ids", []) if cid in flagged_ids]
    return Command(goto="prescreen", update={
        "shortlist": auto_ids + included,
        "events": [human_event("shortlist_review", "Flagged profiles reviewed",
                               f"added {included or 'none'} to the shortlist")],
    })


# ---------------------------------------------------------------------------
# Stage 3 — pre-screen + scheduling + the interview wait
# ---------------------------------------------------------------------------

def prescreen_node(state: RecruitmentState) -> Command[Literal["scheduling", "sourcing", "wrapup"]]:
    settings = get_settings()
    req = state["requisition"]
    cand_by_id = {c.id: c for c in state.get("candidates", [])}
    results, events = [], []
    for cid in state.get("shortlist", []):
        cand = cand_by_id.get(cid)
        if cand is None:
            continue
        result, report = prescreen_candidate(req, cand)
        results.append(result)
        events += _trace_events("prescreen", report)
        events.append(agent_event("prescreen", f"Pre-screened {cand.name}",
                                  f"passed={result.passed}; expects "
                                  f"${result.expected_base:,}; notice {result.notice_days}d"))

    passing = [r.candidate_id for r in results if r.passed]
    if not passing:
        if state.get("sourcing_rounds", 0) < settings.max_sourcing_rounds:
            events.append(system_event("prescreen", "All candidates failed pre-screen — expanding sourcing"))
            return Command(goto="sourcing", update={"prescreen": results, "events": events})
        return Command(goto="wrapup", update={
            "prescreen": results,
            "outcome": "closed_lost",
            "outcome_summary": "Every shortlisted candidate failed logistics pre-screening.",
            "events": events,
        })
    return Command(goto="scheduling", update={
        "prescreen": results,
        "shortlist": [cid for cid in state["shortlist"] if cid in passing],
        "events": events,
    })


def scheduling_node(state: RecruitmentState) -> dict:
    store = get_store()
    req = state["requisition"]
    cand_by_id = {c.id: c for c in state.get("candidates", [])}
    cands = [cand_by_id[cid] for cid in state.get("shortlist", []) if cid in cand_by_id]

    report = schedule_interviews(req, cands, store.interview_rounds(), store.panel())
    events = _trace_events("scheduling", report)

    shortlist_set = set(state.get("shortlist", []))
    bookings = [b for b in store.bookings if b.candidate_id in shortlist_set]
    events.append(agent_event("scheduling", "Interview loop booked",
                              f"{len(bookings)} interview(s) across {len(cands)} candidate(s)"))
    return {"bookings": bookings, "events": events}


def await_feedback_node(state: RecruitmentState) -> dict:
    """Durable wait: the graph parks here until scorecards arrive (webhook/UI)."""
    answer = interrupt(hitl.interview_feedback_payload(state))
    cards = [Scorecard.model_validate(card) for card in answer.get("scorecards", [])]
    return {
        "scorecards": cards,
        "events": [human_event("interviews", "Scorecards submitted",
                               f"{len(cards)} scorecard(s) received from the panel")],
    }


# ---------------------------------------------------------------------------
# Stage 4 — decision support + the hire decision
# ---------------------------------------------------------------------------

def decision_node(state: RecruitmentState) -> dict:
    report = consolidate_feedback(state["requisition"], state.get("scorecards", []),
                                  state.get("screening", []))
    return {
        "recommendations": report.recommendations,
        "comparison_summary": report.comparison_summary,
        "events": [agent_event("decision", "Feedback consolidated",
                               f"{len(report.recommendations)} recommendation(s) prepared")],
    }


def hire_decision_node(state: RecruitmentState) -> Command[Literal["offer", "wrapup"]]:
    declined = set(state.get("declined_ids", []))
    eligible = [r.candidate_id for r in state.get("recommendations", [])
                if r.candidate_id not in declined]
    if not eligible:
        return Command(goto="wrapup", update={
            "outcome": "no_hire",
            "outcome_summary": "No remaining candidates to decide on.",
            "events": [system_event("hire_decision", "No eligible candidates remain")],
        })

    answer = interrupt(hitl.hire_decision_payload(state, eligible))
    chosen = (answer.get("candidate_id") or "none").strip()
    if chosen.lower() == "none" or chosen not in eligible:
        return Command(goto="wrapup", update={
            "outcome": "no_hire",
            "outcome_summary": "Hiring manager decided not to extend an offer.",
            "events": [human_event("hire_decision", "Decision: no hire")],
        })
    return Command(goto="offer", update={
        "chosen_candidate_id": chosen,
        "offer_attempts": 0,
        "escalation_note": "",
        "events": [human_event("hire_decision", "Decision: hire", f"selected {chosen}")],
    })


# ---------------------------------------------------------------------------
# Stage 5 — offer negotiation (with escalation guardrail)
# ---------------------------------------------------------------------------

def offer_node(state: RecruitmentState) -> Command[
    Literal["onboarding", "offer_escalation", "hire_decision", "wrapup"]
]:
    req = state["requisition"]
    chosen_id = state["chosen_candidate_id"]
    cand = next(c for c in state["candidates"] if c.id == chosen_id)
    ceiling = state.get("offer_ceiling") or req.comp_band.base_max
    pres = next((p for p in state.get("prescreen", []) if p.candidate_id == chosen_id), None)
    expected = (pres.expected_base if pres and pres.expected_base else cand.expected_base) or ceiling

    outcome, report = negotiate_offer(req, cand, ceiling, expected, state.get("escalation_note") or None)
    attempts = state.get("offer_attempts", 0) + 1
    events = _trace_events("offer", report)
    events.append(agent_event("offer", f"Negotiation attempt {attempts}: {outcome.status}",
                              outcome.summary))

    if outcome.status == "accepted":
        start = (datetime.now(timezone.utc) + timedelta(days=cand.notice_days + 7)).date().isoformat()
        offer = Offer(candidate_id=chosen_id, base=outcome.final_base or min(expected, ceiling),
                      status=OfferStatus.ACCEPTED, start_date=start,
                      negotiation_log=[outcome.summary])
        return Command(goto="onboarding", update={
            "offer": offer, "offer_attempts": attempts, "events": events,
        })

    if outcome.status == "needs_approval" and attempts <= 2:
        offer = Offer(candidate_id=chosen_id, base=outcome.requested_base or expected,
                      status=OfferStatus.NEEDS_APPROVAL, negotiation_log=[outcome.summary])
        return Command(goto="offer_escalation", update={
            "offer": offer, "offer_attempts": attempts, "events": events,
        })

    # Declined (or escalations exhausted): release the candidate, let the
    # hiring manager pick a backup from the remaining recommendations.
    events.append(system_event("offer", f"Candidate {chosen_id} released",
                               "offer declined or escalation budget exhausted"))
    return Command(goto="hire_decision", update={
        "declined_ids": [chosen_id], "offer_attempts": attempts, "events": events,
    })


def offer_escalation_node(state: RecruitmentState) -> Command[Literal["offer"]]:
    answer = interrupt(hitl.offer_escalation_payload(state))
    if answer.get("approved", False):
        new_ceiling = int(answer.get("new_ceiling") or (state["offer"].base if state.get("offer") else 0))
        return Command(goto="offer", update={
            "offer_ceiling": new_ceiling,
            "escalation_note": f"A human approved an exception: you may now close at up to "
                               f"${new_ceiling:,}. Close promptly.",
            "events": [human_event("offer_escalation", "Offer exception approved",
                                   f"new ceiling ${new_ceiling:,}")],
        })
    return Command(goto="offer", update={
        "escalation_note": "The exception was rejected. Make one final best offer within the "
                           "current ceiling; if the candidate declines, report declined.",
        "events": [human_event("offer_escalation", "Offer exception rejected", "hold the ceiling")],
    })


# ---------------------------------------------------------------------------
# Stage 6 — onboarding + BGV gate
# ---------------------------------------------------------------------------

def onboarding_node(state: RecruitmentState) -> Command[Literal["bgv_review", "wrapup"]]:
    store = get_store()
    req = state["requisition"]
    chosen_id = state["chosen_candidate_id"]
    cand = next(c for c in state["candidates"] if c.id == chosen_id)
    offer = state["offer"]

    report = run_onboarding(req, cand, offer.start_date or "TBC")
    events = _trace_events("onboarding", report)

    # Trust but verify: confirm mandatory artifacts exist in the systems of
    # record; backfill deterministically if the agent missed any.
    case_id = next((cid for cid, case in store.bgv_cases.items()
                    if case["candidate_id"] == chosen_id), None)
    if case_id is None:
        case_id = store.initiate_bgv(chosen_id)
        events.append(system_event("onboarding", "BGV backfilled by orchestrator", case_id))
    if not store.it_tickets:
        store.create_it_ticket(f"Laptop for {cand.name} (start {offer.start_date})", "hardware")
        store.create_it_ticket(f"System & email access for {cand.name}", "access")
        events.append(system_event("onboarding", "IT tickets backfilled by orchestrator"))
    if not store.emails_sent:
        store.send_email(cand.email, "Welcome aboard!", "Signed-offer welcome note")
        events.append(system_event("onboarding", "Welcome email backfilled by orchestrator"))

    status, detail = store.get_bgv_status(case_id)
    plan = OnboardingPlan(
        candidate_id=chosen_id, bgv_case_id=case_id, bgv_status=status, bgv_detail=detail,
        it_tickets=[t["id"] for t in store.it_tickets],
        welcome_emails=[e["subject"] for e in store.emails_sent],
        day1_checklist=["Laptop imaged & shipped", "Accounts provisioned", "Buddy assigned",
                        "Week-1 schedule shared"],
    )
    events.append(agent_event("onboarding", "Pre-boarding executed",
                              f"BGV {status.value}; {len(plan.it_tickets)} ticket(s); "
                              f"{len(plan.welcome_emails)} email(s) queued"))

    if status == BGVStatus.NEEDS_REVIEW:
        return Command(goto="bgv_review", update={"onboarding": plan, "events": events})
    if status == BGVStatus.FAILED:
        return Command(goto="wrapup", update={
            "onboarding": plan, "outcome": "bgv_failed",
            "outcome_summary": f"BGV failed for {cand.name}: {detail}", "events": events,
        })
    return Command(goto="wrapup", update={
        "onboarding": plan, "outcome": "hired",
        "outcome_summary": f"{cand.name} hired; day-1 ready for {offer.start_date}.",
        "events": events,
    })


def bgv_review_node(state: RecruitmentState) -> Command[Literal["wrapup", "hire_decision"]]:
    answer = interrupt(hitl.bgv_review_payload(state))
    plan = state["onboarding"]
    chosen_id = state["chosen_candidate_id"]
    cand = next(c for c in state["candidates"] if c.id == chosen_id)

    if answer.get("cleared", False):
        cleared = plan.model_copy(update={"bgv_status": BGVStatus.CLEAR})
        return Command(goto="wrapup", update={
            "onboarding": cleared, "outcome": "hired",
            "outcome_summary": f"{cand.name} hired; BGV adjudicated clear; day-1 ready.",
            "events": [human_event("bgv_review", "BGV discrepancy cleared",
                                   answer.get("note", ""))],
        })
    failed = plan.model_copy(update={"bgv_status": BGVStatus.FAILED})
    return Command(goto="hire_decision", update={
        "onboarding": failed,
        "declined_ids": [chosen_id],
        "events": [human_event("bgv_review", "BGV adjudicated as failed",
                               answer.get("note", "candidate released; selecting backup"))],
    })


# ---------------------------------------------------------------------------
# Stage 7 — wrap-up
# ---------------------------------------------------------------------------

def wrapup_node(state: RecruitmentState) -> dict:
    outcome = state.get("outcome", "closed_lost")
    events = state.get("events", [])
    agent_actions = sum(1 for e in events if e.actor == "agent")
    human_actions = sum(1 for e in events if e.actor == "human")

    req = state.get("requisition")
    updates: dict = {}
    if req is not None:
        final_status = (RequisitionStatus.CLOSED_FILLED if outcome == "hired"
                        else RequisitionStatus.CLOSED_LOST)
        updates["requisition"] = req.model_copy(update={"status": final_status})

    summary = (f"Outcome: {outcome}. {state.get('outcome_summary', '')} "
               f"Automation footprint: {agent_actions} agent action(s) vs "
               f"{human_actions} human decision(s).")
    updates["outcome"] = outcome
    updates["outcome_summary"] = summary
    updates["events"] = [system_event("wrapup", "Workflow complete", summary)]
    return updates


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph(checkpointer=None):
    builder = StateGraph(RecruitmentState)

    builder.add_node("intake", intake_node)
    builder.add_node("requisition", requisition_node)
    builder.add_node("jd_approval", jd_approval_node)
    builder.add_node("finance_approval", finance_approval_node)
    builder.add_node("sourcing", sourcing_node)
    builder.add_node("screen", screen_node)
    builder.add_node("shortlist_gate", shortlist_gate_node)
    builder.add_node("shortlist_review", shortlist_review_node)
    builder.add_node("prescreen", prescreen_node)
    builder.add_node("scheduling", scheduling_node)
    builder.add_node("await_feedback", await_feedback_node)
    builder.add_node("decision", decision_node)
    builder.add_node("hire_decision", hire_decision_node)
    builder.add_node("offer", offer_node)
    builder.add_node("offer_escalation", offer_escalation_node)
    builder.add_node("onboarding", onboarding_node)
    builder.add_node("bgv_review", bgv_review_node)
    builder.add_node("wrapup", wrapup_node)

    builder.add_edge(START, "intake")
    builder.add_edge("intake", "requisition")
    builder.add_conditional_edges("sourcing", dispatch_screening, ["screen", "shortlist_gate"])
    builder.add_edge("screen", "shortlist_gate")
    builder.add_edge("scheduling", "await_feedback")
    builder.add_edge("await_feedback", "decision")
    builder.add_edge("decision", "hire_decision")
    builder.add_edge("wrapup", END)
    # All remaining routing is dynamic via Command(goto=...) returned by nodes.

    return builder.compile(checkpointer=checkpointer)
