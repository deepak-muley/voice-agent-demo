"""A specialist is the single-agent loop with a smaller tool list.

``run`` proposes, checks the allow-list, runs the handler, and stops.
It returns a result. It does not speak to the caller.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from contracts import Handoff, SpecialistResult
from single_imports import MAX_STEPS, AuditEvent, ModelTurn, ToolCall, trace_json


Handler = Callable  # (**arguments) -> dict. Left unparameterized so Python 3.7 can load it.


def _assistant_tool_message(turn: ModelTurn) -> dict[str, Any]:
    """Same assistant tool_calls shape as the single-agent loop."""
    return {
        "role": "assistant",
        "content": turn.content,
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in turn.tool_calls
        ],
    }


class Specialist:
    def __init__(
        self,
        model: Any,
        name: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        handlers: dict[str, Handler],
        verbose: bool = False,
    ) -> None:
        self.model = model
        self.name = name
        self.system_prompt = system_prompt
        self.tools = tools
        self.handlers = dict(handlers)
        self.verbose = verbose

    def run(self, handoff: Handoff, audit: list[AuditEvent]) -> SpecialistResult:
        # A specialist does not keep the call. The next utterance starts clean.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": self.system_prompt},
            {"role": "user", "content": handoff.as_user_message()},
        ]
        tools_used: list[str] = []
        data: dict[str, Any] = {}

        for _ in range(MAX_STEPS):
            turn = self.model.complete(messages, self.tools)
            if not turn.tool_calls:
                return _finish(turn.content or "", data, tools_used, escalated=False)

            messages.append(_assistant_tool_message(turn))
            for call in turn.tool_calls:
                if call.name == "create_incident":
                    # The model does not get to decide that the caller agreed.
                    call.arguments["confirmed"] = handoff.facts.caller_agreed is True
                tools_used.append(call.name)
                data = _run_tool(self.name, self.handlers, call, audit)
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": json.dumps(data),
                }
                messages.append(tool_message)
                if self.verbose:
                    trace_json(f"{self.name} tool result", tool_message)

        return _finish(
            "I am having trouble finishing that. I will transfer you to a person.",
            data,
            tools_used,
            escalated=True,
        )


def _finish(say_hint: str, data: dict[str, Any], tools_used: list[str], escalated: bool) -> SpecialistResult:
    transferred = bool(data.get("transferred"))
    if escalated or transferred:
        status = "handoff"
    elif data.get("ok") is False or (not data and say_hint):
        status = "need_user"
    else:
        status = "done"
    return SpecialistResult(
        status=status,
        say_hint=say_hint,
        data=data,
        tools_used=list(tools_used),
        escalated=escalated or transferred,
    )


def _run_tool(agent_name: str, handlers: dict[str, Handler], call: ToolCall, audit: list[AuditEvent]) -> dict[str, Any]:
    audit.append(
        AuditEvent("tool_call", {"name": call.name, "arguments": dict(call.arguments), "agent": agent_name})
    )
    handler = handlers.get(call.name)
    if handler is None:
        result: dict[str, Any] = {"ok": False, "error": "unknown_tool"}
    else:
        try:
            result = handler(**call.arguments)
        except TypeError:
            result = {"ok": False, "error": "bad_arguments"}
    audit.append(
        AuditEvent("tool_result", {"name": call.name, "ok": bool(result.get("ok", True)), "agent": agent_name})
    )
    return result
