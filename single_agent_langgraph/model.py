"""Scripted chat model for the LangGraph desk.

``ScriptedModel`` in ``single_agent/`` speaks ``complete(messages, tools)``.
LangGraph speaks ``invoke(messages) -> AIMessage``. This file is that adapter.
``--demo`` and ``--self-check`` use it. ``--live`` uses ``ChatOpenAI`` instead,
and the graph does not change.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_SINGLE = Path(__file__).resolve().parents[1] / "single_agent"
if str(_SINGLE) not in sys.path:
    sys.path.append(str(_SINGLE))

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from model_client import ModelTurn
from voice_agent import TOOLS, ScriptedModel


def to_openai_messages(messages: list[BaseMessage]) -> list[dict[str, Any]]:
    """LangChain messages to the dicts ``ScriptedModel`` and the HTTP client use."""
    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            converted.append({"role": "system", "content": message_text(message)})
        elif isinstance(message, HumanMessage):
            converted.append({"role": "user", "content": message_text(message)})
        elif isinstance(message, ToolMessage):
            converted.append(
                {
                    "role": "tool",
                    "tool_call_id": message.tool_call_id,
                    "content": message_text(message),
                }
            )
        elif isinstance(message, AIMessage):
            item: dict[str, Any] = {"role": "assistant", "content": message_text(message) or None}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"],
                        "type": "function",
                        "function": {
                            "name": call["name"],
                            "arguments": json.dumps(call.get("args") or {}),
                        },
                    }
                    for call in message.tool_calls
                ]
            converted.append(item)
        else:
            converted.append({"role": "user", "content": message_text(message)})
    return converted


def ai_message_from_turn(turn: ModelTurn) -> AIMessage:
    if not turn.tool_calls:
        return AIMessage(content=turn.content or "")
    return AIMessage(
        content=turn.content or "",
        tool_calls=[
            {"name": call.name, "args": call.arguments, "id": call.id, "type": "tool_call"}
            for call in turn.tool_calls
        ],
    )


def message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict):
                parts.append(str(block.get("text") or ""))
        return "".join(parts)
    return str(content)


class ScriptedChatModel(BaseChatModel):
    """Same replies as ``single_agent.voice_agent.ScriptedModel``, as a chat model."""

    trace: bool = False

    @property
    def _llm_type(self) -> str:
        return "scripted-desk"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        del stop, run_manager, kwargs
        turn = ScriptedModel(verbose=self.trace).complete(to_openai_messages(messages), TOOLS)
        return ChatResult(generations=[ChatGeneration(message=ai_message_from_turn(turn))])
