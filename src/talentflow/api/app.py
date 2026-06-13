"""FastAPI control plane for TalentFlow workflows.

Surface a real deployment wires into its systems:

- ``POST /workflows``                  start a lifecycle from a hiring brief
- ``GET  /workflows/{id}``             current stage + pending human action
- ``POST /workflows/{id}/resume``      deliver a human decision / external event
- ``GET  /workflows/{id}/timeline``    full audit trail

**Asynchronous execution.** Running the graph until the next human gate can take
a minute or more of real model calls. Holding an HTTP request open that long
breaks behind corporate proxies and load balancers, so the graph runs in a
background thread: ``POST`` returns immediately with ``status: "running"`` and
the client polls ``GET`` for progress. The graph is checkpointed, so progress
(and the audit timeline) is readable live via a separate read connection while
the run thread writes (SQLite WAL mode makes concurrent read+write safe).

Note: the demo enterprise store is process-global (single-tenant), so the run
lock serialises execution to one workflow at a time.

Cloud Run note: background work needs CPU between requests — deploy with
``--no-cpu-throttling --min-instances=1`` (see deploy/gcp.md).
"""

from __future__ import annotations

import os
import sqlite3
import threading
import time
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

# ---------------------------------------------------------------------------
# Graph + checkpointers
#   - one write connection (the background run thread) in WAL mode
#   - one read connection (request threads) for live state/timeline polling
# ---------------------------------------------------------------------------

_write_graph = None
_read_graph = None
_run_lock = threading.Lock()      # serialises graph.invoke (single-tenant store)
_read_lock = threading.Lock()     # serialises use of the shared read connection

# In-memory run status per workflow: {"state": running|idle|error, "error": str|None}
_runs: dict[str, dict[str, Any]] = {}


def _init_graphs() -> None:
    global _write_graph, _read_graph
    if _write_graph is not None:
        return
    db = get_settings().checkpoint_db
    write_conn = sqlite3.connect(db, check_same_thread=False)
    write_conn.execute("PRAGMA journal_mode=WAL")
    write_conn.execute("PRAGMA busy_timeout=10000")
    _write_graph = build_graph(checkpointer=SqliteSaver(write_conn))

    read_conn = sqlite3.connect(db, check_same_thread=False)
    read_conn.execute("PRAGMA busy_timeout=10000")
    _read_graph = build_graph(checkpointer=SqliteSaver(read_conn))


def _config(workflow_id: str) -> dict:
    return {"configurable": {"thread_id": workflow_id}, "recursion_limit": 80}


# ---------------------------------------------------------------------------
# Background execution
# ---------------------------------------------------------------------------

def _run_graph(workflow_id: str, payload: Any) -> None:
    """Run the graph to the next interrupt/terminus in a background thread."""
    try:
        with _run_lock:
            _write_graph.invoke(payload, _config(workflow_id))
        _runs[workflow_id] = {"state": "idle", "error": None}
    except Exception as exc:  # surface to the client via GET; never crash silently
        _runs[workflow_id] = {"state": "error", "error": f"{type(exc).__name__}: {exc}"}


def _launch(workflow_id: str, payload: Any) -> None:
    _runs[workflow_id] = {"state": "running", "error": None}
    threading.Thread(target=_run_graph, args=(workflow_id, payload), daemon=True).start()


# ---------------------------------------------------------------------------
# Snapshot building
# ---------------------------------------------------------------------------

def _pending_from_state(state) -> dict | None:
    for task in getattr(state, "tasks", ()) or ():
        for intr in getattr(task, "interrupts", ()) or ():
            value = getattr(intr, "value", None)
            if value is not None:
                return value
    return None


def _snapshot(workflow_id: str, values: dict[str, Any], pending: dict | None,
              status: str, error: str | None = None) -> dict:
    requisition = values.get("requisition")
    return {
        "workflow_id": workflow_id,
        "status": status,                      # running | awaiting_human | completed | error
        "error": error,
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


def _read_state(workflow_id: str):
    with _read_lock:
        return _read_graph.get_state(_config(workflow_id))


def _current_snapshot(workflow_id: str) -> dict:
    run = _runs.get(workflow_id, {"state": "idle", "error": None})

    # While the background thread is executing, don't read the run's own writes —
    # but we CAN read the last committed checkpoint for live progress context.
    if run["state"] == "running":
        try:
            state = _read_state(workflow_id)
            values = state.values if state else {}
        except Exception:
            values = {}
        return _snapshot(workflow_id, values, None, "running")

    state = _read_state(workflow_id)
    if state is None or not state.values:
        if run["state"] == "error":
            return _snapshot(workflow_id, {}, None, "error", run["error"])
        raise HTTPException(404, "Unknown workflow id")

    values = state.values
    if run["state"] == "error":
        return _snapshot(workflow_id, values, None, "error", run["error"])

    pending = _pending_from_state(state)
    if pending:
        return _snapshot(workflow_id, values, pending, "awaiting_human")
    if values.get("outcome"):
        return _snapshot(workflow_id, values, None, "completed")
    # Idle with neither interrupt nor outcome (shouldn't normally happen).
    return _snapshot(workflow_id, values, None, "running")


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

class StartWorkflow(BaseModel):
    brief: str
    seed: int | None = None


@app.post("/workflows")
def start_workflow(body: StartWorkflow) -> dict:
    """Kick off the lifecycle. Returns immediately; poll GET for progress."""
    _init_graphs()
    workflow_id = uuid.uuid4().hex[:12]
    reset_store(body.seed)
    _launch(workflow_id, {"brief": body.brief})
    return {"workflow_id": workflow_id, "status": "running", "pending_action": None}


@app.post("/workflows/{workflow_id}/resume")
def resume_workflow(workflow_id: str, decision: dict) -> dict:
    """Deliver a human decision / external event. Returns immediately; poll GET."""
    _init_graphs()
    run = _runs.get(workflow_id)
    if run and run["state"] == "running":
        raise HTTPException(409, "Workflow is still running; wait for the next gate")
    state = _read_state(workflow_id)
    if state is None or not state.values:
        raise HTTPException(404, "Unknown workflow id")
    if not state.next:
        raise HTTPException(409, "Workflow already completed; nothing to resume")
    _launch(workflow_id, Command(resume=decision))
    return {"workflow_id": workflow_id, "status": "running", "pending_action": None}


@app.get("/workflows/{workflow_id}")
def get_workflow(workflow_id: str) -> dict:
    _init_graphs()
    return _current_snapshot(workflow_id)


@app.get("/workflows/{workflow_id}/timeline")
def get_timeline(workflow_id: str) -> list[dict]:
    _init_graphs()
    try:
        state = _read_state(workflow_id)
    except Exception:
        return []
    if state is None or not state.values:
        return []
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
