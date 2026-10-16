"""Offline complete() for each specialist.

Same method as OpenAICompatibleClient. The role decides the reply, the way a
different system prompt would for a live model.
"""

from __future__ import annotations

import json
import re
from typing import Any

from single_imports import ModelTurn, ToolCall, message_from_turn, trace_json


class ScriptedRoleModel:
    def __init__(self, role: str, verbose: bool = False) -> None:
        self.role = role
        self.verbose = verbose

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        if self.verbose:
            trace_json(f"sent to {self.role}", {"messages": messages, "tools": tools})
        turn = getattr(self, f"_{self.role}")(messages)
        if self.verbose:
            trace_json(f"received from {self.role}", message_from_turn(turn))
        return turn

    def _knowledge(self, messages: list[dict[str, Any]]) -> ModelTurn:
        if messages[-1]["role"] == "tool":
            return ModelTurn(content="", tool_calls=[])
        utterance = json.loads(messages[-1]["content"])["utterance"]
        return ModelTurn(None, [ToolCall("call_kb", "search_knowledge", {"query": utterance})])

    def _incident(self, messages: list[dict[str, Any]]) -> ModelTurn:
        if messages[-1]["role"] == "tool":
            return ModelTurn(content="", tool_calls=[])
        payload = json.loads(messages[-1]["content"])
        text = payload["utterance"]
        match = re.search(r"\binc\d+\b", text, re.IGNORECASE)
        # Same order as ScriptedModel: a ticket id wins over a yes.
        if match:
            number = match.group(0).upper()
            return ModelTurn(None, [ToolCall("call_get", "get_incident", {"number": number})])
        if payload.get("caller_agreed"):
            return ModelTurn(
                None,
                [
                    ToolCall(
                        "call_create",
                        "create_incident",
                        {
                            "short_description": "VPN client will not connect before a customer call",
                            "urgency": "high",
                            "confirmed": True,
                        },
                    )
                ],
            )
        return ModelTurn(content="I can open that ticket. Should I go ahead?", tool_calls=[])

    def _escalation(self, messages: list[dict[str, Any]]) -> ModelTurn:
        if messages[-1]["role"] == "tool":
            return ModelTurn(content="", tool_calls=[])
        return ModelTurn(None, [ToolCall("call_esc", "escalate", {"reason": "caller asked for a person"})])
