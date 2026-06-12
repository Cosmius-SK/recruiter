"""The shared, checkpointed state of one recruitment workflow.

Every field is typed; list fields that parallel branches write concurrently
(screening fan-out, audit events) carry an additive reducer so LangGraph can
merge superstep results deterministically.
"""

from __future__ import annotations

import operator
from typing import Annotated, TypedDict

from talentflow.domain.models import (
    AuditEvent,
    Candidate,
    HireRecommendation,
    InterviewBooking,
    Offer,
    OnboardingPlan,
    PrescreenResult,
    Requisition,
    RoleSpec,
    Scorecard,
    ScreeningResult,
)


class RecruitmentState(TypedDict, total=False):
    # Inputs
    brief: str

    # Stage artifacts
    role_spec: RoleSpec
    requisition: Requisition
    jd_feedback: str
    jd_revisions: int
    candidates: list[Candidate]
    screening: Annotated[list[ScreeningResult], operator.add]
    shortlist: list[str]                  # candidate ids, ranked
    prescreen: list[PrescreenResult]
    bookings: list[InterviewBooking]
    scorecards: list[Scorecard]
    recommendations: list[HireRecommendation]
    comparison_summary: str
    chosen_candidate_id: str
    declined_ids: Annotated[list[str], operator.add]
    offer: Offer
    offer_ceiling: int                    # hard guardrail handed to the Offer Agent
    offer_attempts: int
    escalation_note: str
    onboarding: OnboardingPlan

    # Orchestration bookkeeping
    sourcing_rounds: int
    outcome: str                          # hired / closed_lost / no_hire / bgv_failed
    outcome_summary: str
    events: Annotated[list[AuditEvent], operator.add]


class ScreenTask(TypedDict):
    """Payload for one parallel screening branch (dispatched via Send)."""

    requisition: Requisition
    candidate: Candidate
