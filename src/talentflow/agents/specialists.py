"""The eight specialist agents covering the recruitment lifecycle.

Each function wraps one autonomous agent: persona (system prompt) + toolbelt +
typed output contract.  The orchestration graph calls these, never the LLM
directly, so the lifecycle logic stays separate from the agent behaviours.
"""

from __future__ import annotations

import json

from pydantic import BaseModel, Field

from talentflow.agents.base import SpecialistReport, run_specialist, structured_call
from talentflow.domain.models import (
    Candidate,
    HireRecommendation,
    OfferOutcome,
    PrescreenResult,
    Requisition,
    RequisitionDraft,
    RoleSpec,
    Scorecard,
    ScreeningResult,
    SupervisorDecision,
)
from talentflow.tools import (
    OFFER_TOOLS,
    ONBOARDING_TOOLS,
    PRESCREEN_TOOLS,
    REQUISITION_TOOLS,
    SCHEDULING_TOOLS,
    SOURCING_TOOLS,
)

# ---------------------------------------------------------------------------
# Intake: free-text manager brief -> structured RoleSpec
# ---------------------------------------------------------------------------

def parse_brief(brief: str) -> RoleSpec:
    return structured_call(
        system_prompt=(
            "You are the intake assistant of a recruitment platform. Convert the hiring "
            "manager's free-text brief into a precise role specification. Infer a sensible "
            "internal level (L4=senior, L5=staff-track senior, L6=principal) and split skills "
            "into must-have vs nice-to-have. Do not invent requirements the manager did not imply."
        ),
        task=f"Hiring manager brief:\n\n{brief}",
        response_model=RoleSpec,
    )


# ---------------------------------------------------------------------------
# 1. Requisition Agent
# ---------------------------------------------------------------------------

def draft_requisition(role: RoleSpec, feedback: str | None = None) -> tuple[RequisitionDraft, SpecialistReport]:
    task = (
        f"Open a requisition for this role:\n{role.model_dump_json(indent=2)}\n\n"
        "Steps: (1) verify headcount budget for the department, (2) fetch the approved comp "
        "band for the level, (3) pull external market benchmarks, then (4) write a complete, "
        "inclusive, market-calibrated job description in Markdown (title, about, "
        "responsibilities, must-have and nice-to-have qualifications, comp transparency "
        "statement, EEO statement). Propose a maximum base salary: stay within the band unless "
        "market data clearly justifies exceeding it (exceeding the band triggers a finance "
        "approval, so only do it deliberately and say why)."
    )
    if feedback:
        task += f"\n\nThe hiring manager reviewed a previous draft and asked for changes:\n{feedback}\nRevise accordingly."
    report = run_specialist(
        name="requisition_agent",
        system_prompt=(
            "You are the Requisition Agent of an autonomous recruitment system. You turn "
            "approved role specs into budget-checked, market-calibrated job descriptions. "
            "You are precise with numbers and never promise compensation outside what the "
            "tools confirm is available."
        ),
        tools=REQUISITION_TOOLS,
        task=task,
        response_model=RequisitionDraft,
    )
    draft = report.structured if isinstance(report.structured, RequisitionDraft) else RequisitionDraft(
        jd_markdown=report.final_text, proposed_base_max=0, budget_summary="(no structured draft produced)"
    )
    return draft, report


# ---------------------------------------------------------------------------
# 2. Sourcing Agent
# ---------------------------------------------------------------------------

def run_sourcing(requisition: Requisition, round_number: int) -> SpecialistReport:
    task = (
        f"Requisition {requisition.id} ('{requisition.role.title}', level {requisition.role.level}) "
        f"is approved and open. Sourcing round {round_number}.\n\n"
        f"Must-have skills: {', '.join(requisition.role.must_have_skills)}\n"
        f"Job description (for personalising outreach):\n{requisition.jd_markdown[:1500]}\n\n"
        "Do the following: (1) publish the posting to 2-3 relevant job boards, "
        "(2) search the passive talent pool for matching profiles, (3) send a short, "
        "personalised outreach email to each promising passive candidate referencing "
        "something specific from their background. Finish with a one-paragraph summary "
        "of what you did."
    )
    return run_specialist(
        name="sourcing_agent",
        system_prompt=(
            "You are the Sourcing Agent. You maximise qualified top-of-funnel candidate flow: "
            "you publish postings and write outreach that is specific, honest and respectful "
            "of candidates' time. Never spam clearly unqualified people."
        ),
        tools=SOURCING_TOOLS,
        task=task,
    )


# ---------------------------------------------------------------------------
# 3. Screening (per candidate, fanned out in parallel by the orchestrator)
# ---------------------------------------------------------------------------

def screen_candidate(requisition: Requisition, candidate: Candidate) -> ScreeningResult:
    result = structured_call(
        system_prompt=(
            "You are the Screening Agent. Score candidates against the job description "
            "rubric: must-have skill coverage (50%), depth/recency of relevant experience "
            "(30%), domain and scale fit (20%). Recommendation policy: 'advance' if the "
            "profile is a credible fit (roughly score >= 70); 'reject' if clearly not; "
            "'flag_for_human' for unconventional but promising profiles (exceptional "
            "signals despite a non-traditional path) — never silently reject those."
        ),
        task=(
            f"candidate_id: {candidate.id}\n"
            f"Job description:\n{requisition.jd_markdown[:2000]}\n\n"
            f"Candidate profile:\n{candidate.model_dump_json(indent=2, exclude={'bgv_discrepancy'})}\n\n"
            "Score this candidate and return the structured screening result. "
            f"Set candidate_id to exactly '{candidate.id}'."
        ),
        response_model=ScreeningResult,
    )
    result.candidate_id = candidate.id  # never trust the model with the join key
    return result


# ---------------------------------------------------------------------------
# 4. Pre-screen / Outreach Agent (logistics verification chat)
# ---------------------------------------------------------------------------

def prescreen_candidate(requisition: Requisition, candidate: Candidate) -> tuple[PrescreenResult, SpecialistReport]:
    band = requisition.comp_band
    report = run_specialist(
        name="prescreen_agent",
        system_prompt=(
            "You are the Pre-screen Agent. Over chat, you verify logistics with shortlisted "
            "candidates before interviews are scheduled: continued interest, notice period, "
            "salary expectation, and location/work-mode compatibility. Ask one question at a "
            "time using the ask_candidate tool (3-5 questions total). Be warm and concise. "
            "A candidate passes unless they are uninterested, location-incompatible, or their "
            "salary expectation exceeds ~110% of the approved band maximum."
        ),
        tools=PRESCREEN_TOOLS,
        task=(
            f"candidate_id: {candidate.id}\n"
            f"Role: {requisition.role.title} in {requisition.role.location} "
            f"({requisition.role.work_mode}). Approved band max: ${band.base_max:,}.\n"
            f"Run the pre-screen conversation with candidate {candidate.id} ({candidate.name}) "
            f"and report the structured result with candidate_id set to exactly '{candidate.id}'."
        ),
        response_model=PrescreenResult,
    )
    result = report.structured if isinstance(report.structured, PrescreenResult) else PrescreenResult(
        candidate_id=candidate.id, passed=True, notes="(no structured prescreen produced)"
    )
    result.candidate_id = candidate.id
    return result, report


# ---------------------------------------------------------------------------
# 5. Scheduling Agent
# ---------------------------------------------------------------------------

def schedule_interviews(requisition: Requisition, candidates: list[Candidate],
                        rounds: list[str], panel: list[str]) -> SpecialistReport:
    cand_lines = "\n".join(f"- candidate_id: {c.id} ({c.name})" for c in candidates)
    return run_specialist(
        name="scheduling_agent",
        system_prompt=(
            "You are the Scheduling Agent. You read panel calendars and book interview loops "
            "with zero double-bookings. Rules: each candidate gets every round, each round a "
            "different interviewer, never book the same interviewer slot twice (the tool "
            "returns CONFLICT — pick another slot and retry), prefer the earliest viable "
            "slots, and check the candidate's preference once before booking."
        ),
        tools=SCHEDULING_TOOLS,
        task=(
            f"Book the full interview loop for requisition {requisition.id}.\n"
            f"Candidates:\n{cand_lines}\n"
            f"Rounds (in order): {', '.join(rounds)}\n"
            f"Panel: {', '.join(panel)}\n"
            "Fetch availability first, ask each candidate for their preference, then book "
            "every round for every candidate. Finish with a summary table of bookings."
        ),
        max_iterations=24,
    )


# ---------------------------------------------------------------------------
# 6. Decision-support Agent (feedback consolidation)
# ---------------------------------------------------------------------------

class DecisionReport(BaseModel):
    recommendations: list[HireRecommendation] = Field(default_factory=list)
    comparison_summary: str = ""


def consolidate_feedback(requisition: Requisition, scorecards: list[Scorecard],
                         screening: list[ScreeningResult]) -> DecisionReport:
    return structured_call(
        system_prompt=(
            "You are the Decision-support Agent. Consolidate interview scorecards and "
            "screening data into a clear hire/no-hire recommendation per candidate, ranked. "
            "Weight interview verdicts over screening scores. Be honest about weak signals; "
            "the hiring manager makes the final call, your job is a crisp, evidence-based "
            "synthesis."
        ),
        task=(
            f"Role: {requisition.role.title} ({requisition.role.level}).\n\n"
            f"Interview scorecards:\n{json.dumps([s.model_dump() for s in scorecards], indent=2)}\n\n"
            f"Screening results:\n{json.dumps([s.model_dump() for s in screening], indent=2)}\n\n"
            "Produce a recommendation for every candidate that has scorecards."
        ),
        response_model=DecisionReport,
    )


# ---------------------------------------------------------------------------
# 7. Offer Agent
# ---------------------------------------------------------------------------

def negotiate_offer(requisition: Requisition, candidate: Candidate, approved_ceiling: int,
                    expected_base_hint: int | None, escalation_note: str | None = None
                    ) -> tuple[OfferOutcome, SpecialistReport]:
    task = (
        f"candidate_id: {candidate.id}\n"
        f"Negotiate and close the offer for {candidate.name} for requisition {requisition.id} "
        f"({requisition.role.title}).\n"
        f"Approved ceiling (hard limit, already includes any finance approvals): ${approved_ceiling:,}.\n"
        f"Comp band: ${requisition.comp_band.base_min:,} - ${requisition.comp_band.base_max:,}.\n"
        f"Candidate's stated expectation from pre-screen: "
        f"{f'${expected_base_hint:,}' if expected_base_hint else 'unknown'}.\n\n"
        "Open at a fair number (do not lowball below band mid for a strong candidate, do not "
        "open at the ceiling either). Use extend_offer to make each offer and read the "
        "candidate's reply; you may revise upward within the ceiling. Stop after at most 3 "
        "extensions. Report status 'accepted' (with final_base), 'needs_approval' (with the "
        "requested_base the candidate is holding out for, when they will not close within the "
        "ceiling), or 'declined'."
    )
    if escalation_note:
        task += f"\n\nHuman escalation guidance: {escalation_note}"
    report = run_specialist(
        name="offer_agent",
        system_prompt=(
            "You are the Offer Agent. You negotiate offers inside hard compensation guardrails. "
            "You never promise anything above the approved ceiling — exceeding it requires "
            "human approval, which you request by reporting status=needs_approval. You are "
            "candid, fast and fair: a good close protects both candidate experience and the "
            "comp structure."
        ),
        tools=OFFER_TOOLS,
        task=task,
        response_model=OfferOutcome,
    )
    outcome = report.structured if isinstance(report.structured, OfferOutcome) else OfferOutcome(
        status="declined", summary="(no structured offer outcome produced)"
    )
    return outcome, report


# ---------------------------------------------------------------------------
# 8. Onboarding Agent
# ---------------------------------------------------------------------------

def run_onboarding(requisition: Requisition, candidate: Candidate, start_date: str) -> SpecialistReport:
    return run_specialist(
        name="onboarding_agent",
        system_prompt=(
            "You are the Onboarding Agent. The moment an offer is accepted you make day-1 "
            "boring: background verification started, hardware and access tickets raised, "
            "and a warm pre-boarding email drip scheduled so the candidate never goes quiet "
            "during their notice period."
        ),
        tools=ONBOARDING_TOOLS,
        task=(
            f"candidate_id: {candidate.id}\n"
            f"{candidate.name} ({candidate.email}) accepted the offer for "
            f"'{requisition.role.title}'; start date {start_date}.\n"
            "Do all of: (1) initiate BGV and check its status, (2) raise IT tickets for a "
            "laptop (hardware) and for system/email access (access), (3) schedule a welcome "
            "drip of 3 short emails across the notice period (signed-welcome, team intro & "
            "what-to-expect, week-1 logistics). Finish with a day-1 readiness summary."
        ),
        max_iterations=16,
    )


# ---------------------------------------------------------------------------
# Supervisor (adaptive routing at the borderline shortlist juncture)
# ---------------------------------------------------------------------------

def supervisor_review(context: str) -> SupervisorDecision:
    return structured_call(
        system_prompt=(
            "You are the Orchestration Supervisor of a recruitment pipeline. At borderline "
            "junctures you decide whether to proceed with the current candidate slate or run "
            "another sourcing round. Proceeding too thin risks a failed loop; sourcing again "
            "costs ~3-5 days. Decide on the evidence."
        ),
        task=context,
        response_model=SupervisorDecision,
    )
