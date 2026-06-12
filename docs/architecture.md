# TalentFlow — Architecture Deep-Dive

This document explains the design decisions behind TalentFlow for engineers
evaluating or extending it. Read `README.md` first for the lifecycle overview.

## 1. Orchestration model: a governed graph of autonomous agents

There are two failure modes in agentic system design:

1. **One mega-agent** with 30 tools and a prayer — impossible to govern, audit
   or test; routing lives in the prompt.
2. **A rigid pipeline** with LLM calls sprinkled in — no autonomy, no
   adaptivity; it's just RPA with extra steps.

TalentFlow takes the middle path LangGraph is built for:

- **The graph is the supervisor.** Lifecycle policy — what happens after a
  failed pre-screen, how many sourcing rounds are allowed, when finance must
  approve — is *explicit, versionable code* (`orchestrator/graph.py`), not
  prompt text. Routing decisions are returned as `Command(goto=...)` from
  nodes, so each transition is inspectable and testable.
- **Agents are autonomous inside their stage.** Each specialist gets a
  persona, a toolbelt and a task; *it* decides which tools to call, in what
  order, how many times (`agents/base.py` runs the loop). The scheduling agent
  genuinely resolves calendar conflicts by reacting to `CONFLICT` tool
  results; the offer agent genuinely negotiates against the candidate.
- **Adaptive routing where judgement is real.** At the shortlist gate, when
  the qualified pool is borderline, a structured-output LLM supervisor weighs
  slate quality against sourcing delay and decides `proceed` vs
  `expand_sourcing`. Deterministic policy handles the non-judgement cases
  around it (hard floors, loop limits).

## 2. State, checkpointing and durable waits

`RecruitmentState` is a typed, checkpointed dict. Two design rules:

- **Artifacts, not transcripts.** Agent message histories stay *inside* the
  node call; only typed artifacts (Requisition, ScreeningResult, Offer, ...)
  and audit events enter shared state. This keeps checkpoints small and makes
  state diffs meaningful.
- **Reducers where branches merge.** `screening` and `events` are
  `Annotated[list, operator.add]` because parallel screening branches and
  every node append to them concurrently.

Because every node boundary is checkpointed, an `interrupt()` is also the
mechanism for **external event waits**: `await_feedback` parks the workflow
until interview scorecards arrive — minutes in the demo, weeks in reality.
The process can die and restart in between; `Command(resume=...)` against the
same `thread_id` continues exactly where it stopped. The same primitive powers
both HITL approvals and event-driven integration, which is what makes the
FastAPI surface so small.

**Interrupt idempotency rule:** a node containing `interrupt()` re-executes
from its top on resume. Therefore HITL nodes contain *only* pure payload
computation + the interrupt + decision handling. LLM and tool work always
lives in separate nodes that run exactly once per visit.

## 3. Parallelism: screening as map-reduce

`dispatch_screening` emits one `Send("screen", {requisition, candidate})` per
unscreened candidate; LangGraph runs the branches concurrently within a
superstep and the reducer merges their `ScreeningResult`s. The dispatch is
idempotent across sourcing rounds (it skips already-screened ids), so loops
back through sourcing only screen the delta.

## 4. Guardrails live in tools, judgement lives in prompts

The comp ceiling is the canonical example. The Offer Agent is *told* about
the ceiling, but the `extend_offer` tool *enforces* it:

```python
if base > approved_max:
    return "BLOCKED BY POLICY: ... report status=needs_approval"
```

A jailbroken, confused or hallucinating model cannot exceed comp policy,
because the only path to the candidate runs through the tool. Raising the
ceiling requires a human decision at the `offer_escalation` interrupt, which
updates `offer_ceiling` in checkpointed state — i.e. an audited, attributable
state change, not a prompt edit. Apply the same pattern for any
"agent must never X without sign-off" requirement.

## 5. Trust-but-verify (contract enforcement after agent runs)

Agents are probabilistic; lifecycle contracts are not. After an agent
completes, its node verifies the mandatory artifacts against the systems of
record and backfills deterministically when something is missing — e.g.
onboarding checks the BGV vendor and ITSM for the candidate's case/tickets and
creates them itself if the agent failed to. The backfill is logged as a
`system` actor event, so agent reliability is measurable from the audit trail
(count the backfills).

This is also what makes the orchestration **testable without an LLM**: the
test suite swaps Claude for a schema-aware scripted model that performs *no*
tool calls, and the lifecycle still completes correctly because nodes own the
data contracts. The LLM adds quality; the graph guarantees integrity.

## 6. Human-in-the-loop as typed contracts

Every gate's `interrupt()` payload (`orchestrator/hitl.py`) carries:

- `type` — gate discriminator (drives UI rendering and resume validation)
- `title`, `assignee`, `summary` — what an approval inbox displays
- gate-specific evidence (JD markdown, flagged-candidate cards, negotiation
  log, BGV detail) — enough to decide *without leaving the inbox*
- `expects` — the schema of the human's answer

The six gates and their answer contracts:

| Gate | Resume payload |
|---|---|
| `jd_approval` | `{approved: bool, feedback?: str}` |
| `finance_approval` | `{approved: bool, approved_max?: int}` |
| `shortlist_review` | `{include_ids: [candidate_id]}` |
| `interview_feedback` | `{scorecards: [Scorecard]}` |
| `hire_decision` | `{candidate_id: str \| "none"}` |
| `offer_escalation` | `{approved: bool, new_ceiling?: int}` |
| `bgv_review` | `{cleared: bool, note?: str}` |

## 7. The audit trail

`AuditEvent(ts, stage, actor, action, detail)` with `actor ∈ {agent, human,
system}` is appended by every node — including one event per tool call from
each specialist's trace. This yields, for free:

- complete decision provenance per candidate (compliance / adverse-action
  requirements),
- the automation-vs-human-touch ratio reported at wrap-up,
- per-stage cycle-time measurement (timestamps bracket each stage),
- agent reliability metrics (system-actor backfill counts).

## 8. Model strategy

One factory (`talentflow/llm.py`) owns model construction, and every agent
talks to it through LangChain's common interface (`bind_tools` +
`with_structured_output`), so the provider is pure configuration:

- **Gemini** (`gemini-2.5-flash` via `langchain-google-genai`) — testing-phase
  default; the free tier makes full lifecycle runs effectively free.
- **Claude** (`claude-opus-4-8` via `langchain-anthropic`, wrapping the
  official Anthropic SDK) — production target. Sampling parameters are
  deliberately not set — Opus 4.7+ rejects them.

Resolution: explicit `TALENTFLOW_PROVIDER`, else whichever of
`GOOGLE_API_KEY` / `ANTHROPIC_API_KEY` is present (Google wins). The same
seam supports per-agent model tiering (a cheap model for high-volume parallel
screening, a frontier model for negotiation and decision synthesis) as a
config change, not a refactor. Every agent terminates in a structured output
(Pydantic schema), so downstream routing never parses prose.

## 9. Extension points

| Want | Change |
|---|---|
| Real ATS/HRIS/BGV/etc. | Reimplement the corresponding methods in `integrations/store.py` (or split it into per-system adapters); tool signatures stay |
| Slack/Teams approvals | Render `hitl.py` payloads as modals; POST the answer to `/workflows/{id}/resume` |
| More gates | Add a node with `interrupt()` + a payload builder + a `Command` route |
| Multi-position / campaign hiring | One `thread_id` per requisition; fan out at the API layer |
| Postgres / horizontal scale | Swap `SqliteSaver` for `PostgresSaver` in `api/app.py` |
| Tracing & evals | Set `LANGSMITH_TRACING=true`; the graph is fully traced node-by-node |
| Candidate-facing chat | The pre-screen/negotiation simulator in `store.py` is the seam where real email/WhatsApp threads plug in |
