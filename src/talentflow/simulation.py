"""Helpers that simulate the human-paced parts of the lifecycle for demos/tests.

In production, interview scorecards arrive from real interviewers via the ATS
and resume the parked workflow through the API. For demos and tests this module
synthesizes plausible scorecards, deterministically derived from screening
scores.
"""

from __future__ import annotations

from talentflow.domain.models import InterviewBooking, ScreeningResult
from talentflow.integrations.store import get_store


def _verdict_for(rating: int) -> str:
    if rating >= 5:
        return "strong_hire"
    if rating >= 4:
        return "hire"
    if rating >= 2:
        return "no_hire"
    return "strong_no_hire"


def synthesize_scorecards(
    shortlist: list[str],
    screening: list[ScreeningResult],
    bookings: list[InterviewBooking] | list[dict] | None = None,
) -> list[dict]:
    """Build one scorecard dict per booked round (or per shortlist x round)."""
    score_by_id = {s.candidate_id: s.score for s in screening}

    slots: list[tuple[str, str, str]] = []  # (candidate_id, round_name, interviewer)
    for booking in bookings or []:
        data = booking.model_dump() if isinstance(booking, InterviewBooking) else dict(booking)
        slots.append((data["candidate_id"], data["round_name"], data["interviewer"]))
    if not slots:
        store = get_store()
        rounds, panel = store.interview_rounds(), store.panel()
        for cid in shortlist:
            for idx, round_name in enumerate(rounds):
                slots.append((cid, round_name, panel[idx % len(panel)]))

    cards = []
    for cid, round_name, interviewer in slots:
        score = score_by_id.get(cid, 60)
        rating = max(1, min(5, round(score / 20)))
        cards.append({
            "candidate_id": cid,
            "round_name": round_name,
            "interviewer": interviewer,
            "rating": rating,
            "verdict": _verdict_for(rating),
            "notes": f"(simulated) Performance consistent with screening signal ({score}/100).",
        })
    return cards
