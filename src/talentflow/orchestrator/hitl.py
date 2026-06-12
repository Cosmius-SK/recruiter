"""Human-in-the-loop payload builders.

Each builder produces the JSON-serializable payload surfaced when the graph
``interrupt()``s — what an approval inbox (web UI, Slack modal, email action)
would render for the human, plus the shape of the answer it expects back.
"""

from __future__ import annotations

from talentflow.orchestrator.state import RecruitmentState


def jd_approval_payload(state: RecruitmentState) -> dict:
    req = state["requisition"]
    return {
        "type": "jd_approval",
        "title": f"Approve job description — {req.role.title}",
        "assignee": req.role.hiring_manager,
        "summary": req.budget_summary,
        "jd_markdown": req.jd_markdown,
        "proposed_base_max": req.proposed_base_max,
        "comp_band_max": req.comp_band.base_max,
        "expects": {"approved": "bool", "feedback": "str (required when approved=false)"},
    }


def finance_approval_payload(state: RecruitmentState) -> dict:
    req = state["requisition"]
    return {
        "type": "finance_approval",
        "title": f"Comp exception — {req.role.title}",
        "assignee": "Finance Partner",
        "summary": (f"Requisition proposes a base ceiling of ${req.proposed_base_max:,}, above the "
                    f"approved band max of ${req.comp_band.base_max:,}."),
        "proposed_base_max": req.proposed_base_max,
        "comp_band_max": req.comp_band.base_max,
        "expects": {"approved": "bool", "approved_max": "int (ceiling granted when approved=true)"},
    }


def shortlist_review_payload(state: RecruitmentState, auto_ids: list[str], flagged_ids: list[str]) -> dict:
    cand_by_id = {c.id: c for c in state.get("candidates", [])}
    screen_by_id = {s.candidate_id: s for s in state.get("screening", [])}

    def card(cid: str) -> dict:
        c, s = cand_by_id.get(cid), screen_by_id.get(cid)
        return {
            "candidate_id": cid,
            "name": c.name if c else cid,
            "score": s.score if s else None,
            "rationale": s.rationale if s else "",
            "resume_summary": c.resume_summary if c else "",
        }

    return {
        "type": "shortlist_review",
        "title": "Review flagged candidate profiles",
        "assignee": state["requisition"].role.hiring_manager,
        "summary": ("The screening agent flagged unconventional-but-promising profiles for human "
                    "review. Decide whether to add them to the auto-shortlist."),
        "auto_shortlist": [card(cid) for cid in auto_ids],
        "flagged": [card(cid) for cid in flagged_ids],
        "expects": {"include_ids": "list[str] — flagged candidate ids to add to the shortlist"},
    }


def interview_feedback_payload(state: RecruitmentState) -> dict:
    return {
        "type": "interview_feedback",
        "title": "Interviews in progress — awaiting scorecards",
        "assignee": "Interview panel",
        "summary": ("The loop is booked. The workflow is parked durably and resumes the moment "
                    "scorecards are submitted (panel availability and interviews themselves are "
                    "human-paced and cannot be fast-tracked)."),
        "bookings": [b.model_dump() for b in state.get("bookings", [])],
        "shortlist": state.get("shortlist", []),
        "expects": {"scorecards": "list[Scorecard] — one per booked round per candidate"},
    }


def hire_decision_payload(state: RecruitmentState, eligible: list[str]) -> dict:
    recs = [r for r in state.get("recommendations", []) if r.candidate_id in eligible]
    cand_by_id = {c.id: c for c in state.get("candidates", [])}
    return {
        "type": "hire_decision",
        "title": "Hire / no-hire decision",
        "assignee": state["requisition"].role.hiring_manager,
        "summary": state.get("comparison_summary", ""),
        "recommendations": [
            {**r.model_dump(), "name": cand_by_id[r.candidate_id].name if r.candidate_id in cand_by_id else r.candidate_id}
            for r in recs
        ],
        "expects": {"candidate_id": "str — chosen candidate id, or 'none' to close without hire"},
    }


def offer_escalation_payload(state: RecruitmentState) -> dict:
    req = state["requisition"]
    cand = next((c for c in state.get("candidates", []) if c.id == state.get("chosen_candidate_id")), None)
    offer = state.get("offer")
    return {
        "type": "offer_escalation",
        "title": f"Offer exception — {cand.name if cand else state.get('chosen_candidate_id')}",
        "assignee": "Recruiter + Finance Partner",
        "summary": (f"Candidate will not close within the approved ceiling of "
                    f"${state.get('offer_ceiling', 0):,}; they are holding out for "
                    f"${offer.base:,}." if offer else "Offer negotiation requires an exception."),
        "requested_base": offer.base if offer else None,
        "current_ceiling": state.get("offer_ceiling"),
        "comp_band_max": req.comp_band.base_max,
        "negotiation_log": offer.negotiation_log if offer else [],
        "expects": {"approved": "bool", "new_ceiling": "int (granted ceiling when approved=true)"},
    }


def bgv_review_payload(state: RecruitmentState) -> dict:
    plan = state.get("onboarding")
    cand = next((c for c in state.get("candidates", []) if c.id == state.get("chosen_candidate_id")), None)
    return {
        "type": "bgv_review",
        "title": f"BGV adjudication — {cand.name if cand else state.get('chosen_candidate_id')}",
        "assignee": "HR / Legal",
        "summary": "The background-verification vendor returned a discrepancy that requires human adjudication.",
        "bgv_case_id": plan.bgv_case_id if plan else None,
        "detail": plan.bgv_detail if plan else "",
        "expects": {"cleared": "bool", "note": "str"},
    }
