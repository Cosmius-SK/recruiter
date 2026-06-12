"""Domain entities shared by agents, gates and integrations.

Everything that crosses an agent boundary is a typed Pydantic model so that
LLM outputs are schema-validated (via structured outputs) and the workflow
state survives checkpointing/resumption byte-for-byte.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Position / requisition
# ---------------------------------------------------------------------------

class WorkMode(StrEnum):
    ONSITE = "onsite"
    HYBRID = "hybrid"
    REMOTE = "remote"


class RoleSpec(BaseModel):
    """Structured interpretation of the hiring manager's free-text brief."""

    title: str
    department: str
    level: str = Field(description="Internal job level, e.g. L3..L6")
    location: str
    work_mode: WorkMode = WorkMode.HYBRID
    headcount: int = 1
    must_have_skills: list[str] = Field(default_factory=list)
    nice_to_have_skills: list[str] = Field(default_factory=list)
    responsibilities: list[str] = Field(default_factory=list)
    hiring_manager: str = "Hiring Manager"
    target_start: str | None = None
    notes: str | None = None


class CompBand(BaseModel):
    level: str
    location: str
    currency: str = "USD"
    base_min: int
    base_mid: int
    base_max: int

    def contains(self, base: int) -> bool:
        return self.base_min <= base <= self.base_max


class RequisitionDraft(BaseModel):
    """What the Requisition Agent must produce for human sign-off."""

    jd_markdown: str = Field(description="Complete, market-calibrated job description in Markdown")
    proposed_base_max: int = Field(description="Maximum base salary the team proposes to offer")
    budget_summary: str = Field(description="One-paragraph summary of the budget/band check performed")


class RequisitionStatus(StrEnum):
    DRAFT = "draft"
    PENDING_APPROVAL = "pending_approval"
    OPEN = "open"
    CLOSED_FILLED = "closed_filled"
    CLOSED_LOST = "closed_lost"


class Requisition(BaseModel):
    id: str
    role: RoleSpec
    comp_band: CompBand
    jd_markdown: str = ""
    proposed_base_max: int = 0
    approved_base_max: int = 0  # may exceed band max only after finance approval
    budget_ok: bool = False
    budget_summary: str = ""
    status: RequisitionStatus = RequisitionStatus.DRAFT


# ---------------------------------------------------------------------------
# Candidates & screening
# ---------------------------------------------------------------------------

class CandidateSource(StrEnum):
    INBOUND = "inbound"
    SOURCED = "sourced"
    REFERRAL = "referral"
    INTERNAL = "internal"


class Candidate(BaseModel):
    id: str
    name: str
    email: str
    source: CandidateSource = CandidateSource.INBOUND
    years_experience: float = 0
    skills: list[str] = Field(default_factory=list)
    location: str = ""
    current_base: int | None = None
    expected_base: int | None = None
    notice_days: int = 30
    resume_summary: str = ""
    unconventional: bool = False  # e.g. non-traditional background worth a human look
    bgv_discrepancy: str | None = None  # seeded data used by the mock BGV vendor


class ScreeningResult(BaseModel):
    candidate_id: str
    score: int = Field(ge=0, le=100, description="Fit score against the JD rubric, 0-100")
    strengths: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    recommendation: Literal["advance", "reject", "flag_for_human"] = "reject"
    rationale: str = ""


class PrescreenResult(BaseModel):
    candidate_id: str
    interested: bool = True
    notice_days: int = 30
    expected_base: int = 0
    location_compatible: bool = True
    passed: bool = True
    notes: str = ""


# ---------------------------------------------------------------------------
# Interviews & decision
# ---------------------------------------------------------------------------

class InterviewBooking(BaseModel):
    candidate_id: str
    round_name: str
    interviewer: str
    start_iso: str
    duration_minutes: int = 60


class Scorecard(BaseModel):
    candidate_id: str
    round_name: str
    interviewer: str
    rating: int = Field(ge=1, le=5)
    verdict: Literal["strong_hire", "hire", "no_hire", "strong_no_hire"]
    notes: str = ""


class HireRecommendation(BaseModel):
    candidate_id: str
    recommendation: Literal["hire", "no_hire"]
    confidence: Literal["low", "medium", "high"] = "medium"
    summary: str = ""


# ---------------------------------------------------------------------------
# Offer
# ---------------------------------------------------------------------------

class OfferStatus(StrEnum):
    DRAFT = "draft"
    EXTENDED = "extended"
    ACCEPTED = "accepted"
    DECLINED = "declined"
    NEEDS_APPROVAL = "needs_approval"


class Offer(BaseModel):
    candidate_id: str
    base: int
    bonus_pct: int = 10
    equity_units: int = 0
    start_date: str | None = None
    status: OfferStatus = OfferStatus.DRAFT
    negotiation_log: list[str] = Field(default_factory=list)


class OfferOutcome(BaseModel):
    """Structured result the Offer Agent must report back to the orchestrator."""

    status: Literal["accepted", "declined", "needs_approval"]
    final_base: int = 0
    requested_base: int = Field(
        default=0,
        description="If approval is needed: the base the candidate is holding out for",
    )
    summary: str = ""


# ---------------------------------------------------------------------------
# Onboarding
# ---------------------------------------------------------------------------

class BGVStatus(StrEnum):
    PENDING = "pending"
    CLEAR = "clear"
    NEEDS_REVIEW = "needs_review"
    FAILED = "failed"


class OnboardingPlan(BaseModel):
    candidate_id: str
    bgv_case_id: str | None = None
    bgv_status: BGVStatus = BGVStatus.PENDING
    bgv_detail: str = ""
    it_tickets: list[str] = Field(default_factory=list)
    welcome_emails: list[str] = Field(default_factory=list)
    day1_checklist: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Orchestration support
# ---------------------------------------------------------------------------

class SupervisorDecision(BaseModel):
    """LLM supervisor verdict at the borderline shortlist juncture."""

    action: Literal["proceed", "expand_sourcing"]
    rationale: str = ""


class AuditEvent(BaseModel):
    """Append-only trail of who (agent/human/system) did what, when."""

    ts: datetime = Field(default_factory=utcnow)
    stage: str
    actor: Literal["agent", "human", "system"]
    action: str
    detail: str = ""


def agent_event(stage: str, action: str, detail: str = "") -> AuditEvent:
    return AuditEvent(stage=stage, actor="agent", action=action, detail=detail)


def human_event(stage: str, action: str, detail: str = "") -> AuditEvent:
    return AuditEvent(stage=stage, actor="human", action=action, detail=detail)


def system_event(stage: str, action: str, detail: str = "") -> AuditEvent:
    return AuditEvent(stage=stage, actor="system", action=action, detail=detail)
