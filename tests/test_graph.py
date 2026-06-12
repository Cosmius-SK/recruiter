"""Orchestration tests: graph structure + the full lifecycle run offline.

The end-to-end test drives the real graph (real nodes, real interrupts, real
checkpointing, real mock integrations) with a scripted model standing in for
Claude — proving the orchestration logic independently of the LLM.
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from talentflow.domain.models import BGVStatus, OfferStatus, RequisitionStatus
from talentflow.integrations.store import get_store, reset_store
from talentflow.orchestrator.graph import build_graph
from talentflow.simulation import synthesize_scorecards

EXPECTED_NODES = {
    "intake", "requisition", "jd_approval", "finance_approval", "sourcing", "screen",
    "shortlist_gate", "shortlist_review", "prescreen", "scheduling", "await_feedback",
    "decision", "hire_decision", "offer", "offer_escalation", "onboarding",
    "bgv_review", "wrapup",
}


def test_graph_compiles_with_all_lifecycle_stages():
    graph = build_graph()
    nodes = set(graph.get_graph().nodes.keys()) - {"__start__", "__end__"}
    assert nodes == EXPECTED_NODES


def test_full_lifecycle_offline(stub_llm):
    reset_store(seed=7)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "e2e"}, "recursion_limit": 80}

    result = graph.invoke(
        {"brief": "Need a senior backend engineer (Python/Go/K8s), Engineering, Austin, L5."},
        config,
    )

    gates_seen: list[str] = []
    answers = {
        "jd_approval": lambda p, v: {"approved": True},
        "shortlist_review": lambda p, v: {
            "include_ids": [c["candidate_id"] for c in p["flagged"]]},
        "interview_feedback": lambda p, v: {
            "scorecards": synthesize_scorecards(v["shortlist"], v["screening"],
                                                v.get("bookings", []))},
        "hire_decision": lambda p, v: {"candidate_id": "cand-priya"},
        "offer_escalation": lambda p, v: {"approved": True, "new_ceiling": 195_000},
        "bgv_review": lambda p, v: {"cleared": True, "note": "verified offline"},
    }

    while result.get("__interrupt__"):
        payload = result["__interrupt__"][0].value
        gates_seen.append(payload["type"])
        assert len(gates_seen) < 20, "interrupt loop did not converge"
        values = graph.get_state(config).values
        result = graph.invoke(Command(resume=answers[payload["type"]](payload, values)), config)

    # Exactly the right human gates fired, in lifecycle order.
    assert gates_seen == [
        "jd_approval",          # hiring manager signs off the JD
        "shortlist_review",     # flagged unconventional profile reviewed
        "interview_feedback",   # durable wait for panel scorecards
        "hire_decision",        # manager makes the call
        "offer_escalation",     # out-of-band comp requires human approval
        "bgv_review",           # BGV discrepancy adjudicated
    ]

    values = graph.get_state(config).values
    assert values["outcome"] == "hired"
    assert values["requisition"].status == RequisitionStatus.CLOSED_FILLED
    assert values["offer"].status == OfferStatus.ACCEPTED
    assert values["offer"].base == 192_000
    assert values["offer"].start_date
    assert values["onboarding"].bgv_status == BGVStatus.CLEAR
    assert values["chosen_candidate_id"] == "cand-priya"

    # The flagged candidate made it onto the shortlist via the human gate.
    assert "cand-dev" in values["shortlist"]

    # Day-1 readiness artifacts exist in the systems of record.
    store = get_store()
    assert store.it_tickets and store.emails_sent and store.bgv_cases

    # Audit trail captures both agent automation and human decisions.
    actors = {e.actor for e in values["events"]}
    assert {"agent", "human", "system"} <= actors


def test_budget_block_short_circuits_lifecycle(stub_llm, monkeypatch):
    """A department with no headcount budget must close the workflow at intake."""
    from talentflow.domain.models import RoleSpec

    def design_role(text: str) -> RoleSpec:
        return RoleSpec(title="Product Designer", department="Design", level="L5",
                        location="Austin, TX")

    stub_llm.factories["RoleSpec"] = design_role
    reset_store(seed=7)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "budget"}, "recursion_limit": 40}

    result = graph.invoke({"brief": "Need a product designer."}, config)
    assert not result.get("__interrupt__"), "no human gate should fire on a budget block"
    assert result["outcome"] == "closed_lost"
    assert "budget" in result["outcome_summary"].lower()


def test_jd_revision_loop(stub_llm):
    """Rejecting the JD routes back through the Requisition Agent with feedback."""
    reset_store(seed=7)
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "revise"}, "recursion_limit": 40}

    result = graph.invoke({"brief": "Senior backend engineer, Engineering, L5."}, config)
    assert result["__interrupt__"][0].value["type"] == "jd_approval"

    result = graph.invoke(
        Command(resume={"approved": False, "feedback": "Tone down the buzzwords."}), config)
    payload = result["__interrupt__"][0].value
    assert payload["type"] == "jd_approval"  # redrafted and back for approval

    values = graph.get_state(config).values
    assert values["jd_revisions"] == 1
    assert values["jd_feedback"] == "Tone down the buzzwords."
