"""End-to-end demo: run the whole lifecycle from a hiring brief in one terminal.

    talentflow-demo                # interactive: you play every human approver
    talentflow-demo --auto         # the humans are simulated (hands-free run)

Requires an LLM key: GOOGLE_API_KEY (Gemini, default for testing) or
ANTHROPIC_API_KEY (Claude) — in the environment or a local .env file. The
enterprise systems are seeded mocks; the agents, orchestration, interrupts
and audit trail are the real thing.
"""

from __future__ import annotations

import argparse

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from rich.console import Console
from rich.markdown import Markdown
from rich.panel import Panel
from rich.prompt import Confirm, Prompt
from rich.table import Table

from talentflow.integrations.store import reset_store
from talentflow.orchestrator.graph import build_graph
from talentflow.simulation import synthesize_scorecards

console = Console()

DEFAULT_BRIEF = (
    "We need a senior backend engineer for the platform team in Austin, TX (hybrid). "
    "Core stack is Python and Go on Kubernetes/AWS at serious scale; they will own the "
    "event-pipeline rewrite and mentor two mid-level engineers. Engineering department, "
    "one headcount, level L5, ideally starting within two months. Hiring manager: Asha Menon."
)


def render_pending(payload: dict) -> None:
    title = f"[bold yellow]⏸ HUMAN GATE[/] — {payload.get('title', payload.get('type'))}"
    body = f"[bold]Assignee:[/] {payload.get('assignee', '—')}\n\n{payload.get('summary', '')}"
    console.print(Panel(body, title=title, border_style="yellow"))
    if payload.get("type") == "jd_approval" and payload.get("jd_markdown"):
        console.print(Panel(Markdown(payload["jd_markdown"][:2500]), title="Job description (draft)"))
    if payload.get("type") == "shortlist_review":
        for card in payload.get("flagged", []):
            console.print(f"  [magenta]flagged[/] {card['name']} (score {card['score']}): {card['rationale'][:160]}")
    if payload.get("type") == "hire_decision":
        for rec in payload.get("recommendations", []):
            console.print(f"  [cyan]{rec['recommendation']:8}[/] {rec['name']} "
                          f"({rec['confidence']}) — {rec['summary'][:140]}")


def auto_answer(payload: dict, values: dict) -> dict:
    """Simulated approvers: sensible default decision at every gate."""
    kind = payload.get("type")
    if kind == "jd_approval":
        return {"approved": True}
    if kind == "finance_approval":
        return {"approved": True, "approved_max": payload.get("proposed_base_max")}
    if kind == "shortlist_review":
        return {"include_ids": [c["candidate_id"] for c in payload.get("flagged", [])]}
    if kind == "interview_feedback":
        cards = synthesize_scorecards(values.get("shortlist", []), values.get("screening", []),
                                      values.get("bookings", []))
        return {"scorecards": cards}
    if kind == "hire_decision":
        recs = payload.get("recommendations", [])
        hires = [r for r in recs if r["recommendation"] == "hire"] or recs
        return {"candidate_id": hires[0]["candidate_id"] if hires else "none"}
    if kind == "offer_escalation":
        return {"approved": True, "new_ceiling": payload.get("requested_base")}
    if kind == "bgv_review":
        return {"cleared": True, "note": "Verified offline with the previous employer; dates reconciled."}
    return {}


def interactive_answer(payload: dict, values: dict) -> dict:
    kind = payload.get("type")
    if kind == "jd_approval":
        if Confirm.ask("Approve this JD?", default=True):
            return {"approved": True}
        return {"approved": False, "feedback": Prompt.ask("What should change?")}
    if kind == "finance_approval":
        if Confirm.ask(f"Approve ceiling of ${payload['proposed_base_max']:,}?", default=True):
            return {"approved": True, "approved_max": payload["proposed_base_max"]}
        return {"approved": False}
    if kind == "shortlist_review":
        include = []
        for card in payload.get("flagged", []):
            if Confirm.ask(f"Add flagged candidate {card['name']} to the shortlist?", default=True):
                include.append(card["candidate_id"])
        return {"include_ids": include}
    if kind == "interview_feedback":
        console.print("[dim]Interviews are human-paced — simulating panel scorecards...[/]")
        return auto_answer(payload, values)
    if kind == "hire_decision":
        options = [r["candidate_id"] for r in payload.get("recommendations", [])] + ["none"]
        choice = Prompt.ask("Who do you want to hire?", choices=options, default=options[0])
        return {"candidate_id": choice}
    if kind == "offer_escalation":
        if Confirm.ask(f"Approve an exception up to ${payload.get('requested_base', 0):,}?", default=True):
            return {"approved": True, "new_ceiling": payload.get("requested_base")}
        return {"approved": False}
    if kind == "bgv_review":
        cleared = Confirm.ask(f"Clear this discrepancy? ({payload.get('detail', '')[:120]})", default=True)
        return {"cleared": cleared, "note": "adjudicated via demo CLI"}
    return {}


def render_timeline(values: dict) -> None:
    table = Table(title="Audit timeline", show_lines=False)
    table.add_column("when", style="dim", no_wrap=True)
    table.add_column("stage", style="cyan", no_wrap=True)
    table.add_column("actor", no_wrap=True)
    table.add_column("what")
    for event in values.get("events", []):
        actor_style = {"agent": "green", "human": "yellow", "system": "blue"}[event.actor]
        table.add_row(event.ts.strftime("%H:%M:%S"), event.stage,
                      f"[{actor_style}]{event.actor}[/]", f"{event.action} — {event.detail[:110]}")
    console.print(table)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the TalentFlow lifecycle demo.")
    parser.add_argument("--auto", action="store_true", help="simulate every human approver")
    parser.add_argument("--brief", default=DEFAULT_BRIEF, help="hiring manager brief to start from")
    args = parser.parse_args()

    reset_store()
    graph = build_graph(checkpointer=InMemorySaver())
    config = {"configurable": {"thread_id": "demo"}, "recursion_limit": 80}

    console.print(Panel.fit("[bold]TalentFlow[/] — position-to-onboarding, agents doing the work,\n"
                            "humans making the calls.", border_style="green"))
    console.print(Panel(args.brief, title="Hiring manager brief"))

    result = graph.invoke({"brief": args.brief}, config)
    gates = 0
    while result.get("__interrupt__"):
        payload = result["__interrupt__"][0].value
        values = graph.get_state(config).values
        render_pending(payload)
        answer = auto_answer(payload, values) if args.auto else interactive_answer(payload, values)
        gates += 1
        console.print(f"[yellow]→ human decision delivered:[/] {str(answer)[:160]}\n")
        result = graph.invoke(Command(resume=answer), config)

    values = graph.get_state(config).values
    render_timeline(values)
    outcome = values.get("outcome", "unknown")
    style = "green" if outcome == "hired" else "red"
    console.print(Panel(f"[bold {style}]{outcome.upper()}[/]\n\n{values.get('outcome_summary', '')}\n"
                        f"Human gates exercised: {gates}", title="Result", border_style=style))


if __name__ == "__main__":
    main()
