"""Mock enterprise systems behind clean adapter interfaces.

Every external system TalentFlow touches in production (Workday/SAP HRIS,
Greenhouse/Lever ATS, LinkedIn/Naukri job boards, Google/Outlook calendars,
HireRight/First Advantage BGV, Jira/ServiceNow ITSM, email/WhatsApp comms)
is represented here as a deterministic in-memory implementation seeded with
realistic data.  The agent tools in ``talentflow.tools`` talk only to this
interface, so swapping a mock for a real connector is a one-file change.
"""

from __future__ import annotations

import itertools
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from talentflow.config import get_settings
from talentflow.domain.models import (
    BGVStatus,
    Candidate,
    CandidateSource,
    CompBand,
    InterviewBooking,
)

# ---------------------------------------------------------------------------
# HRIS: budgets + compensation bands
# ---------------------------------------------------------------------------

_COMP_BANDS: dict[tuple[str, str], CompBand] = {
    ("L4", "any"): CompBand(level="L4", location="any", base_min=110_000, base_mid=130_000, base_max=150_000),
    ("L5", "any"): CompBand(level="L5", location="any", base_min=140_000, base_mid=165_000, base_max=185_000),
    ("L6", "any"): CompBand(level="L6", location="any", base_min=175_000, base_mid=205_000, base_max=235_000),
}

_DEPARTMENT_BUDGETS: dict[str, int] = {
    "engineering": 3,   # open headcount remaining
    "data": 2,
    "product": 1,
    "design": 0,        # exhausted — exercises the budget-fail path
}

_MARKET_BENCHMARKS: dict[str, str] = {
    "backend": "P50 base $158k / P75 $176k for senior backend (platform) in major US hubs; "
               "Go and Kubernetes experience commands a 5-8% premium this quarter.",
    "data": "P50 base $150k / P75 $172k for senior data engineers; streaming (Kafka/Flink) "
            "skills are the strongest differentiator.",
    "frontend": "P50 base $148k / P75 $168k for senior frontend; design-system experience trending.",
    "default": "P50 base $150k / P75 $170k for comparable senior software roles in major US hubs.",
}


# ---------------------------------------------------------------------------
# Seeded candidate universe (inbound applicants + passive talent pool)
# ---------------------------------------------------------------------------

def _seed_candidates() -> list[Candidate]:
    return [
        Candidate(
            id="cand-priya", name="Priya Raman", email="priya.raman@example.com",
            source=CandidateSource.INBOUND, years_experience=9,
            skills=["python", "go", "kubernetes", "postgresql", "aws", "system design"],
            location="Austin, TX", current_base=175_000, expected_base=192_000, notice_days=30,
            resume_summary="Staff-adjacent backend engineer; led a 6-person platform team, "
                           "migrated a monolith to event-driven services handling 40k rps.",
            bgv_discrepancy="Employment dates at previous employer differ by ~2 months from resume.",
        ),
        Candidate(
            id="cand-marcus", name="Marcus Webb", email="marcus.webb@example.com",
            source=CandidateSource.INBOUND, years_experience=7,
            skills=["python", "django", "postgresql", "redis", "docker"],
            location="Denver, CO", current_base=150_000, expected_base=168_000, notice_days=14,
            resume_summary="Senior backend engineer at a fintech scale-up; strong API design and "
                           "payments-domain experience, some Kubernetes exposure.",
        ),
        Candidate(
            id="cand-elena", name="Elena Sokolova", email="elena.sokolova@example.com",
            source=CandidateSource.SOURCED, years_experience=8,
            skills=["go", "kubernetes", "grpc", "terraform", "gcp", "system design"],
            location="Remote (EST)", current_base=170_000, expected_base=180_000, notice_days=45,
            resume_summary="Infrastructure-leaning backend engineer; built multi-region service mesh "
                           "and an internal deployment platform used by 200 engineers.",
        ),
        Candidate(
            id="cand-dev", name="Dev Patel", email="dev.patel@example.com",
            source=CandidateSource.INBOUND, years_experience=3.5,
            skills=["python", "rust", "kubernetes", "open source", "distributed systems"],
            location="Seattle, WA", current_base=125_000, expected_base=150_000, notice_days=21,
            resume_summary="Bootcamp graduate turned maintainer of a 9k-star open-source job queue; "
                           "no big-company experience but exceptional public engineering record.",
            unconventional=True,
        ),
        Candidate(
            id="cand-sara", name="Sara Lindqvist", email="sara.lindqvist@example.com",
            source=CandidateSource.REFERRAL, years_experience=11,
            skills=["java", "spring", "oracle", "soap"],
            location="Boston, MA", current_base=160_000, expected_base=175_000, notice_days=60,
            resume_summary="Enterprise Java veteran; deep in legacy modernization programmes, "
                           "limited cloud-native exposure.",
        ),
        Candidate(
            id="cand-tom", name="Tom Okafor", email="tom.okafor@example.com",
            source=CandidateSource.INBOUND, years_experience=2,
            skills=["javascript", "react", "node"],
            location="Chicago, IL", current_base=95_000, expected_base=120_000, notice_days=14,
            resume_summary="Frontend-leaning full-stack developer, 2 years at an agency.",
        ),
        Candidate(
            id="cand-aiko", name="Aiko Tanaka", email="aiko.tanaka@example.com",
            source=CandidateSource.SOURCED, years_experience=6,
            skills=["python", "kafka", "flink", "airflow", "snowflake"],
            location="San Francisco, CA", current_base=165_000, expected_base=185_000, notice_days=30,
            resume_summary="Streaming-data specialist; built CDC pipelines feeding ML feature stores.",
        ),
    ]


_INTERVIEW_PANEL = ["Asha Menon (EM)", "Diego Alvarez (Staff Eng)", "Wei Chen (Sr Eng)", "Hannah Roth (HRBP)"]
_INTERVIEW_ROUNDS = ["Technical Deep-Dive", "System Design", "Values & Collaboration"]


# ---------------------------------------------------------------------------
# The store
# ---------------------------------------------------------------------------

@dataclass
class EnterpriseStore:
    seed: int = 42
    candidates: dict[str, Candidate] = field(default_factory=dict)
    postings: list[dict] = field(default_factory=list)
    outreach_log: list[dict] = field(default_factory=list)
    bookings: list[InterviewBooking] = field(default_factory=list)
    booked_slots: set[tuple[str, str]] = field(default_factory=set)  # (interviewer, start_iso)
    bgv_cases: dict[str, dict] = field(default_factory=dict)
    it_tickets: list[dict] = field(default_factory=list)
    emails_sent: list[dict] = field(default_factory=list)
    negotiation_rounds: dict[str, int] = field(default_factory=dict)
    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))

    def __post_init__(self) -> None:
        self.rng = random.Random(self.seed)
        for cand in _seed_candidates():
            self.candidates[cand.id] = cand

    # -- HRIS ---------------------------------------------------------------

    def check_budget(self, department: str, headcount: int) -> tuple[bool, str]:
        dept = department.strip().lower()
        remaining = _DEPARTMENT_BUDGETS.get(dept)
        if remaining is None:
            return False, f"Department '{department}' not found in the headcount plan."
        if remaining >= headcount:
            return True, (f"Approved headcount available: {remaining} open slot(s) in "
                          f"{department}; requesting {headcount}.")
        return False, (f"Insufficient budget: {remaining} open slot(s) in {department}, "
                       f"requested {headcount}. Requires VP exception.")

    def get_comp_band(self, level: str, location: str = "any") -> CompBand | None:
        return _COMP_BANDS.get((level.upper(), "any"))

    def get_market_benchmark(self, title: str) -> str:
        t = title.lower()
        for key, value in _MARKET_BENCHMARKS.items():
            if key in t:
                return value
        return _MARKET_BENCHMARKS["default"]

    # -- ATS / job boards / talent pool --------------------------------------

    def publish_posting(self, requisition_id: str, boards: list[str]) -> str:
        self.postings.append({"requisition_id": requisition_id, "boards": boards})
        return f"Posting for {requisition_id} published to: {', '.join(boards)}."

    def fetch_inbound_applications(self) -> list[Candidate]:
        return [c for c in self.candidates.values() if c.source == CandidateSource.INBOUND]

    def search_talent_pool(self, skills: list[str]) -> list[Candidate]:
        wanted = {s.strip().lower() for s in skills}
        hits = []
        for cand in self.candidates.values():
            if cand.source == CandidateSource.INBOUND:
                continue
            overlap = wanted & {s.lower() for s in cand.skills}
            if overlap:
                hits.append(cand)
        return hits

    def log_outreach(self, candidate_id: str, message: str) -> str:
        self.outreach_log.append({"candidate_id": candidate_id, "message": message})
        return f"Outreach email queued to {candidate_id}."

    # -- Candidate communications simulator -----------------------------------
    # Stands in for the human on the other side of email/WhatsApp.  Replies are
    # derived deterministically from the seeded profile so demos and tests are
    # reproducible.

    def ask_candidate(self, candidate_id: str, question: str) -> str:
        cand = self.candidates.get(candidate_id)
        if cand is None:
            return "ERROR: unknown candidate id."
        q = question.lower()
        if "notice" in q or "join" in q or "start" in q or "available" in q:
            return (f"My notice period is {cand.notice_days} days; I could start "
                    f"about {cand.notice_days + 7} days after signing.")
        if "salary" in q or "compensation" in q or "expect" in q or "pay" in q:
            return (f"I'm currently at ${cand.current_base:,} base and looking for "
                    f"around ${cand.expected_base:,} base.")
        if "location" in q or "relocat" in q or "remote" in q or "hybrid" in q or "office" in q:
            return (f"I'm based in {cand.location}. Hybrid works for me if the office "
                    f"is commutable; otherwise I'd need a remote arrangement.")
        if "interest" in q or "role" in q or "why" in q:
            return ("Yes, I'm interested — the role looks like a strong match for what "
                    "I want to do next. Happy to proceed.")
        if "slot" in q or "time" in q or "schedule" in q or "interview" in q:
            return "Weekday mornings work best for me, but I can be flexible with a few days' notice."
        return "Sure — could you clarify what you'd like to know?"

    # -- Calendar -------------------------------------------------------------

    def get_panel_availability(self, days_ahead: int = 5) -> dict[str, list[str]]:
        """Free slots per interviewer over the coming days (deterministic)."""
        base = datetime.now(timezone.utc).replace(minute=0, second=0, microsecond=0)
        availability: dict[str, list[str]] = {}
        for idx, interviewer in enumerate(_INTERVIEW_PANEL):
            slots = []
            for day in range(1, days_ahead + 1):
                for hour in (9 + idx % 2, 11, 14 + idx % 3):
                    slot = (base + timedelta(days=day)).replace(hour=hour)
                    iso = slot.isoformat()
                    if (interviewer, iso) not in self.booked_slots:
                        slots.append(iso)
            availability[interviewer] = slots[:6]
        return availability

    def book_interview(self, candidate_id: str, round_name: str, interviewer: str,
                       start_iso: str, duration_minutes: int = 60) -> str:
        key = (interviewer, start_iso)
        if key in self.booked_slots:
            return f"CONFLICT: {interviewer} already has a booking at {start_iso}. Pick another slot."
        self.booked_slots.add(key)
        booking = InterviewBooking(candidate_id=candidate_id, round_name=round_name,
                                   interviewer=interviewer, start_iso=start_iso,
                                   duration_minutes=duration_minutes)
        self.bookings.append(booking)
        return (f"Booked '{round_name}' for {candidate_id} with {interviewer} at "
                f"{start_iso} ({duration_minutes} min). Invites sent to both parties.")

    def interview_rounds(self) -> list[str]:
        return list(_INTERVIEW_ROUNDS)

    def panel(self) -> list[str]:
        return list(_INTERVIEW_PANEL)

    # -- Offer negotiation simulator -------------------------------------------

    def candidate_offer_response(self, candidate_id: str, base: int) -> str:
        """Deterministic negotiation behaviour driven by the seeded profile."""
        cand = self.candidates.get(candidate_id)
        if cand is None:
            return "ERROR: unknown candidate id."
        expected = cand.expected_base or base
        rounds = self.negotiation_rounds.get(candidate_id, 0)
        self.negotiation_rounds[candidate_id] = rounds + 1
        if base >= int(expected * 0.97):
            return f"ACCEPT: Thank you — ${base:,} base works for me. Please send the letter."
        if rounds >= 2:
            if base >= int(expected * 0.90):
                return f"ACCEPT: ${base:,} is a little under my target but I'm excited about the team — I accept."
            return "DECLINE: I appreciate the offer but the gap to my expectation is too large."
        counter = max(expected, int(base * 1.04))
        return (f"COUNTER: I was hoping for closer to ${counter:,} base given my current "
                f"compensation. Can you improve the offer?")

    # -- BGV vendor -------------------------------------------------------------

    def initiate_bgv(self, candidate_id: str) -> str:
        case_id = f"BGV-{next(self._counter):04d}"
        cand = self.candidates.get(candidate_id)
        if cand and cand.bgv_discrepancy:
            status, detail = BGVStatus.NEEDS_REVIEW, cand.bgv_discrepancy
        else:
            status, detail = BGVStatus.CLEAR, "Identity, education and employment verified clean."
        self.bgv_cases[case_id] = {"candidate_id": candidate_id, "status": status, "detail": detail}
        return case_id

    def get_bgv_status(self, case_id: str) -> tuple[BGVStatus, str]:
        case = self.bgv_cases.get(case_id)
        if case is None:
            return BGVStatus.PENDING, "Unknown case id."
        return case["status"], case["detail"]

    # -- ITSM -------------------------------------------------------------------

    def create_it_ticket(self, summary: str, category: str) -> str:
        ticket_id = f"IT-{next(self._counter):04d}"
        self.it_tickets.append({"id": ticket_id, "summary": summary, "category": category})
        return ticket_id

    # -- Email / drip campaigns ---------------------------------------------------

    def send_email(self, to: str, subject: str, body_summary: str) -> str:
        self.emails_sent.append({"to": to, "subject": subject, "body": body_summary})
        return f"Email '{subject}' queued to {to}."


# ---------------------------------------------------------------------------
# Module-level store handle (configured per workflow run / test)
# ---------------------------------------------------------------------------

_store: EnterpriseStore | None = None


def get_store() -> EnterpriseStore:
    global _store
    if _store is None:
        _store = EnterpriseStore(seed=get_settings().random_seed)
    return _store


def reset_store(seed: int | None = None) -> EnterpriseStore:
    global _store
    _store = EnterpriseStore(seed=seed if seed is not None else get_settings().random_seed)
    return _store
