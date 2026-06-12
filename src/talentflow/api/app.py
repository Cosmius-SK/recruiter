"""FastAPI control plane for TalentFlow workflows.

This is the surface a real deployment wires into its systems:

- ``POST /workflows``                  start a lifecycle from a hiring brief
- ``GET  /workflows/{id}``             current stage + pending human action
- ``POST /workflows/{id}/resume``      deliver a human decision / external event
                                       (approval UI, Slack action, ATS webhook)
- ``GET  /workflows/{id}/timeline``    full audit trail

Workflows are checkpointed in SQLite, so the process can restart at any point
and every parked workflow resumes exactly where it stopped — including waits
that take weeks (e.g. interviews during a notice period).

Note: the demo enterprise store is process-global (single-tenant). Run one
workflow at a time against it, or swap in real connectors for multi-tenancy.
"""

from __future__ import annotations

import os
import sqlite3
import uuid
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command
from pydantic import BaseModel

from talentflow.config import get_settings
from talentflow.integrations.store import reset_store
from talentflow.orchestrator.graph import build_graph

app = FastAPI(
    title="TalentFlow",
    description="Autonomous, human-governed recruitment-lifecycle orchestration.",
    version="0.1.0",
)

_graph = None


def get_graph():
    global _graph
    if _graph is None:
        settings = get_settings()
        conn = sqlite3.connect(settings.checkpoint_db, check_same_thread=False)
        _graph = build_graph(checkpointer=SqliteSaver(conn))
    return _graph


def _config(workflow_id: str) -> dict:
    return {"configurable": {"thread_id": workflow_id}, "recursion_limit": 80}


def _pending_interrupt(result: dict[str, Any]) -> dict | None:
    interrupts = result.get("__interrupt__") or []
    if not interrupts:
        return None
    first = interrupts[0]
    return getattr(first, "value", first)


def _snapshot(workflow_id: str, values: dict[str, Any], pending: dict | None) -> dict:
    requisition = values.get("requisition")
    return {
        "workflow_id": workflow_id,
        "status": "awaiting_human" if pending else ("completed" if values.get("outcome") else "running"),
        "pending_action": pending,
        "requisition_id": requisition.id if requisition else None,
        "stage_artifacts": {
            "role": values["role_spec"].model_dump() if values.get("role_spec") else None,
            "shortlist": values.get("shortlist"),
            "chosen_candidate_id": values.get("chosen_candidate_id"),
            "offer": values["offer"].model_dump() if values.get("offer") else None,
            "onboarding": values["onboarding"].model_dump() if values.get("onboarding") else None,
        },
        "outcome": values.get("outcome"),
        "outcome_summary": values.get("outcome_summary"),
    }


class StartWorkflow(BaseModel):
    brief: str
    seed: int | None = None


@app.post("/workflows")
def start_workflow(body: StartWorkflow) -> dict:
    """Kick off the lifecycle; runs autonomously until the first human gate."""
    workflow_id = uuid.uuid4().hex[:12]
    reset_store(body.seed)
    graph = get_graph()
    result = graph.invoke({"brief": body.brief}, _config(workflow_id))
    return _snapshot(workflow_id, result, _pending_interrupt(result))


@app.post("/workflows/{workflow_id}/resume")
def resume_workflow(workflow_id: str, decision: dict) -> dict:
    """Deliver a human decision or external event to a parked workflow.

    The body must match the ``expects`` contract of the pending action, e.g.
    ``{"approved": true}`` for jd_approval or ``{"scorecards": [...]}`` for
    interview_feedback.
    """
    graph = get_graph()
    state = graph.get_state(_config(workflow_id))
    if state is None or not state.values:
        raise HTTPException(404, "Unknown workflow id")
    if not state.next:
        raise HTTPException(409, "Workflow already completed; nothing to resume")
    result = graph.invoke(Command(resume=decision), _config(workflow_id))
    return _snapshot(workflow_id, result, _pending_interrupt(result))


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> dict:
    graph = get_graph()
    state = graph.get_state(_config(workflow_id))
    if state is None or not state.values:
        raise HTTPException(404, "Unknown workflow id")
    pending = None
    for task in getattr(state, "tasks", ()):  # surface the parked interrupt, if any
        for intr in getattr(task, "interrupts", ()):
            pending = getattr(intr, "value", None)
            break
    return _snapshot(workflow_id, state.values, pending)


@app.get("/workflows/{workflow_id}/timeline")
def get_timeline(workflow_id: str) -> list[dict]:
    graph = get_graph()
    state = graph.get_state(_config(workflow_id))
    if state is None or not state.values:
        raise HTTPException(404, "Unknown workflow id")
    return [e.model_dump(mode="json") for e in state.values.get("events", [])]


# ---------------------------------------------------------------------------
# Product website (optional): if a `website/` directory exists relative to the
# working directory (as in the Docker image and the repo root), serve it at /.
# API routes above take precedence; this enables single-service deployments
# (e.g. Cloud Run) where one HTTPS URL serves both the site and the API.
# ---------------------------------------------------------------------------

_website_dir = os.environ.get("TALENTFLOW_WEBSITE_DIR", "website")
if os.path.isdir(_website_dir):
    app.mount("/", StaticFiles(directory=_website_dir, html=True), name="website")
