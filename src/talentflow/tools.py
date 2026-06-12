"""LangChain tools that expose the enterprise systems to the agents.

Tools are the security boundary: each specialist agent only receives the
toolbelt for its stage, and every guardrail (comp-band ceilings, booking
conflicts, budget checks) is enforced *inside the tool*, not left to the
model's good behaviour.
"""

from __future__ import annotations

import json

from langchain_core.tools import tool

from talentflow.integrations.store import get_store


# ---------------------------------------------------------------------------
# Requisition Agent toolbelt
# ---------------------------------------------------------------------------

@tool
def check_budget(department: str, headcount: int = 1) -> str:
    """Check the HRIS headcount plan for available budget in a department."""
    ok, detail = get_store().check_budget(department, headcount)
    return f"{'APPROVED' if ok else 'BLOCKED'}: {detail}"


@tool
def get_comp_band(level: str) -> str:
    """Fetch the approved compensation band (base salary min/mid/max) for an internal level."""
    band = get_store().get_comp_band(level)
    if band is None:
        return f"No comp band defined for level '{level}'. Valid levels: L4, L5, L6."
    return (f"Level {band.level}: base ${band.base_min:,} (min) / ${band.base_mid:,} (mid) / "
            f"${band.base_max:,} (max) {band.currency}. Offers above max require finance approval.")


@tool
def get_market_benchmark(title: str) -> str:
    """Get current external market compensation and demand data for a job title."""
    return get_store().get_market_benchmark(title)


REQUISITION_TOOLS = [check_budget, get_comp_band, get_market_benchmark]


# ---------------------------------------------------------------------------
# Sourcing Agent toolbelt
# ---------------------------------------------------------------------------

@tool
def publish_job_posting(requisition_id: str, boards: list[str]) -> str:
    """Publish the approved job posting to external job boards (e.g. LinkedIn, Indeed, Wellfound)."""
    return get_store().publish_posting(requisition_id, boards)


@tool
def fetch_inbound_applications() -> str:
    """List candidates who have applied to the posting through the ATS."""
    cands = get_store().fetch_inbound_applications()
    return json.dumps([c.model_dump(include={"id", "name", "years_experience", "skills",
                                             "location", "resume_summary"}) for c in cands], indent=2)


@tool
def search_talent_pool(skills: list[str]) -> str:
    """Semantic search of the passive talent pool / past applicants by skills."""
    cands = get_store().search_talent_pool(skills)
    if not cands:
        return "No passive candidates matched those skills."
    return json.dumps([c.model_dump(include={"id", "name", "years_experience", "skills",
                                             "location", "resume_summary"}) for c in cands], indent=2)


@tool
def send_outreach_email(candidate_id: str, message: str) -> str:
    """Send a personalised outreach email to a passive candidate inviting them to apply."""
    return get_store().log_outreach(candidate_id, message)


SOURCING_TOOLS = [publish_job_posting, fetch_inbound_applications, search_talent_pool, send_outreach_email]


# ---------------------------------------------------------------------------
# Pre-screen / Outreach Agent toolbelt
# ---------------------------------------------------------------------------

@tool
def ask_candidate(candidate_id: str, question: str) -> str:
    """Ask the candidate one question over the chat/email channel and get their reply.

    Use this to verify logistics: notice period, salary expectation, location or
    work-mode compatibility, and continued interest in the role.
    """
    return get_store().ask_candidate(candidate_id, question)


PRESCREEN_TOOLS = [ask_candidate]


# ---------------------------------------------------------------------------
# Scheduling Agent toolbelt
# ---------------------------------------------------------------------------

@tool
def get_panel_availability(days_ahead: int = 5) -> str:
    """Read the interview panel's calendars and return free slots (ISO timestamps) per interviewer."""
    avail = get_store().get_panel_availability(days_ahead)
    return json.dumps(avail, indent=2)


@tool
def book_interview(candidate_id: str, round_name: str, interviewer: str,
                   start_iso: str, duration_minutes: int = 60) -> str:
    """Book one interview round: blocks the interviewer's calendar and sends invites.

    Returns CONFLICT if the slot is taken — pick a different slot and retry.
    """
    return get_store().book_interview(candidate_id, round_name, interviewer, start_iso, duration_minutes)


SCHEDULING_TOOLS = [get_panel_availability, ask_candidate, book_interview]


# ---------------------------------------------------------------------------
# Offer Agent toolbelt
# ---------------------------------------------------------------------------

@tool
def extend_offer(candidate_id: str, base: int, approved_max: int) -> str:
    """Extend (or re-extend) an offer at the given base salary and return the candidate's reply.

    GUARDRAIL: base must not exceed approved_max — the orchestrator supplies the
    currently approved ceiling; offers above it are rejected by this tool and must
    instead be escalated to a human via your final report (status=needs_approval).
    """
    if base > approved_max:
        return (f"BLOCKED BY POLICY: ${base:,} exceeds the approved ceiling of ${approved_max:,}. "
                f"Do NOT retry above the ceiling. If the candidate will not accept within the "
                f"ceiling, finish and report status=needs_approval with the requested base.")
    return get_store().candidate_offer_response(candidate_id, base)


OFFER_TOOLS = [get_comp_band, extend_offer]


# ---------------------------------------------------------------------------
# Onboarding Agent toolbelt
# ---------------------------------------------------------------------------

@tool
def initiate_bgv(candidate_id: str) -> str:
    """Open a background-verification case with the BGV vendor. Returns the case id."""
    case_id = get_store().initiate_bgv(candidate_id)
    return f"BGV case opened: {case_id}"


@tool
def get_bgv_status(case_id: str) -> str:
    """Check the status of a BGV case: clear, needs_review (human must adjudicate) or failed."""
    status, detail = get_store().get_bgv_status(case_id)
    return f"{status.value}: {detail}"


@tool
def create_it_ticket(summary: str, category: str) -> str:
    """Raise an ITSM ticket (categories: hardware, access, software) for day-1 readiness."""
    ticket_id = get_store().create_it_ticket(summary, category)
    return f"Ticket created: {ticket_id} [{category}] {summary}"


@tool
def schedule_welcome_email(candidate_email: str, subject: str, body_summary: str) -> str:
    """Schedule a pre-boarding / welcome drip email to the future hire."""
    return get_store().send_email(candidate_email, subject, body_summary)


ONBOARDING_TOOLS = [initiate_bgv, get_bgv_status, create_it_ticket, schedule_welcome_email]
