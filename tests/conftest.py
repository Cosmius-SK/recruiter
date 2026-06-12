"""Test fixtures, including a scripted stand-in for the Claude model.

The stub lets the *entire orchestration graph* run offline: deterministic
structured outputs per schema, no tool calls (the orchestrator's
trust-but-verify backfills cover the contracts), no network, no API key.
"""

from __future__ import annotations

import re

import pytest
from langchain_core.messages import AIMessage

from talentflow.agents.specialists import DecisionReport
from talentflow.domain.models import (
    HireRecommendation,
    OfferOutcome,
    PrescreenResult,
    RequisitionDraft,
    RoleSpec,
    ScreeningResult,
    SupervisorDecision,
)

# Deterministic screening behaviour for the seeded candidate universe.
SCORES: dict[str, tuple[int, str]] = {
    "cand-priya": (92, "advance"),
    "cand-elena": (88, "advance"),
    "cand-marcus": (80, "advance"),
    "cand-dev": (65, "flag_for_human"),
    "cand-sara": (55, "reject"),
    "cand-aiko": (60, "reject"),
    "cand-tom": (30, "reject"),
}

EXPECTED_BASE = {
    "cand-priya": 192_000,
    "cand-elena": 180_000,
    "cand-marcus": 168_000,
    "cand-dev": 150_000,
}


def _cand_id(text: str) -> str:
    match = re.search(r"candidate_id: (\S+)", text)
    return match.group(1) if match else "cand-unknown"


def _build_factories():
    def role_spec(text: str) -> RoleSpec:
        return RoleSpec(
            title="Senior Backend Engineer", department="Engineering", level="L5",
            location="Austin, TX", must_have_skills=["python", "go", "kubernetes"],
            hiring_manager="Asha Menon",
        )

    def requisition_draft(text: str) -> RequisitionDraft:
        return RequisitionDraft(
            jd_markdown="# Senior Backend Engineer\n\n(stub JD)",
            proposed_base_max=185_000,
            budget_summary="Budget confirmed; ceiling within the L5 band.",
        )

    def screening(text: str) -> ScreeningResult:
        cid = _cand_id(text)
        score, rec = SCORES.get(cid, (40, "reject"))
        return ScreeningResult(candidate_id=cid, score=score, recommendation=rec,
                               rationale=f"stub screening for {cid}")

    def prescreen(text: str) -> PrescreenResult:
        cid = _cand_id(text)
        return PrescreenResult(candidate_id=cid, interested=True, notice_days=30,
                               expected_base=EXPECTED_BASE.get(cid, 170_000),
                               location_compatible=True, passed=True)

    def decision(text: str) -> DecisionReport:
        ids = sorted(set(re.findall(r"cand-[a-z]+", text)))
        recs = [HireRecommendation(
            candidate_id=cid,
            recommendation="hire" if cid == "cand-priya" else "no_hire",
            confidence="high", summary=f"stub recommendation for {cid}",
        ) for cid in ids]
        return DecisionReport(recommendations=recs, comparison_summary="stub comparison")

    def offer(text: str) -> OfferOutcome:
        if "human escalation guidance" in text.lower():
            return OfferOutcome(status="accepted", final_base=192_000,
                                summary="closed after human-approved exception")
        return OfferOutcome(status="needs_approval", requested_base=192_000,
                            summary="candidate holding out above the approved ceiling")

    def supervisor(text: str) -> SupervisorDecision:
        return SupervisorDecision(action="proceed", rationale="stub")

    return {
        "RoleSpec": role_spec,
        "RequisitionDraft": requisition_draft,
        "ScreeningResult": screening,
        "PrescreenResult": prescreen,
        "DecisionReport": decision,
        "OfferOutcome": offer,
        "SupervisorDecision": supervisor,
    }


class _StubStructured:
    def __init__(self, factory):
        self.factory = factory

    def invoke(self, messages):
        text = "\n".join(
            content if isinstance(content := getattr(m, "content", m), str) else str(content)
            for m in messages
        )
        return self.factory(text)


class StubChatModel:
    """Schema-aware scripted model: no tools, deterministic structured outputs."""

    def __init__(self):
        self.factories = _build_factories()

    def bind_tools(self, tools):
        return self

    def invoke(self, messages) -> AIMessage:
        return AIMessage(content="(stub) acknowledged — no tool actions taken")

    def with_structured_output(self, schema):
        name = getattr(schema, "__name__", str(schema))
        if name not in self.factories:
            raise AssertionError(f"No stub factory registered for schema '{name}'")
        return _StubStructured(self.factories[name])


@pytest.fixture
def stub_llm(monkeypatch) -> StubChatModel:
    stub = StubChatModel()
    monkeypatch.setattr("talentflow.llm.get_chat_model", lambda: stub)
    return stub
