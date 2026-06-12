"""The specialist-agent runtime: an explicit tool-use loop + schema extraction.

We run the loop by hand (rather than a prebuilt helper) for three reasons:
- the orchestrator gets a full tool trace for the audit log,
- guardrails live in tools and the loop enforces a hard iteration budget,
- every specialist ends by emitting a *typed* report (Pydantic schema via
  structured outputs), which is the contract the orchestration graph routes on.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Sequence, Type, TypeVar

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from pydantic import BaseModel

from talentflow import llm as llm_module

TModel = TypeVar("TModel", bound=BaseModel)

MAX_TOOL_ITERATIONS = 12


@dataclass
class SpecialistReport:
    """What a specialist hands back to the orchestrator."""

    final_text: str
    structured: BaseModel | None = None
    tool_trace: list[str] = field(default_factory=list)


def _text_of(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def run_specialist(
    *,
    name: str,
    system_prompt: str,
    tools: Sequence,
    task: str,
    response_model: Type[TModel] | None = None,
    max_iterations: int = MAX_TOOL_ITERATIONS,
) -> SpecialistReport:
    """Run one specialist agent to completion and return its report.

    The agent decides autonomously which tools to call and in what order; the
    loop ends when the model stops requesting tools or the iteration budget is
    exhausted.  If ``response_model`` is given, a final structured-output pass
    converts the agent's work into that schema.
    """
    model = llm_module.get_chat_model()
    bound = model.bind_tools(list(tools)) if tools else model
    tool_map = {t.name: t for t in tools}

    messages: list[BaseMessage] = [SystemMessage(content=system_prompt), HumanMessage(content=task)]
    trace: list[str] = []

    for _ in range(max_iterations):
        ai: AIMessage = bound.invoke(messages)
        messages.append(ai)
        tool_calls = getattr(ai, "tool_calls", None) or []
        if not tool_calls:
            break
        for call in tool_calls:
            tool_obj = tool_map.get(call["name"])
            if tool_obj is None:
                result = f"ERROR: unknown tool '{call['name']}'"
            else:
                try:
                    result = tool_obj.invoke(call["args"])
                except Exception as exc:  # surface tool failures to the model, never crash the run
                    result = f"TOOL ERROR: {exc}"
            trace.append(
                f"{name} -> {call['name']}({json.dumps(call['args'], default=str)[:200]}) "
                f"=> {str(result)[:240]}"
            )
            messages.append(ToolMessage(content=str(result), tool_call_id=call["id"]))

    final_text = _text_of(messages[-1])

    structured: BaseModel | None = None
    if response_model is not None:
        extractor = model.with_structured_output(response_model)
        structured = extractor.invoke(
            messages
            + [HumanMessage(content="Now emit your final report in the required structure, "
                                    "based strictly on the work above.")]
        )

    return SpecialistReport(final_text=final_text, structured=structured, tool_trace=trace)


def structured_call(*, system_prompt: str, task: str, response_model: Type[TModel]) -> TModel:
    """Single-shot, schema-validated LLM call (no tools)."""
    model = llm_module.get_chat_model()
    extractor = model.with_structured_output(response_model)
    return extractor.invoke([SystemMessage(content=system_prompt), HumanMessage(content=task)])
