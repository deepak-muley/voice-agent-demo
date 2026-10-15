"""Conversation loop for a voice agent.

Redacts the transcript, calls `model.complete`, runs a handler only when its
name was supplied by the caller, and returns speech-ready text. The system
prompt, tool schemas, and handlers are arguments, so this loop is not tied
to one desk.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from model_client import ModelTurn, ToolCall


Handler = Callable  # (**arguments) -> dict. Kept unparameterized so Python 3.7 can load it.
MAX_STEPS = 4

_REDACTIONS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "[REDACTED_SSN]"),
    (re.compile(r"\b(?:\d[ -]?){13,19}\b"), "[REDACTED_CARD]"),
    (re.compile(r"(?i)\b(password|passcode|pin)\s*(is|=|:)\s*\S+"), r"\1 is [REDACTED_SECRET]"),
]

_INCIDENT_ID = re.compile(r"\bINC\d+\b", re.IGNORECASE)
_MARKDOWN = re.compile(r"[*`#_>]")
_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")


@dataclass
class SpeechReply:
    text: str
    tools_used: list[str]
    escalated: bool = False


@dataclass
class AuditEvent:
    kind: str
    detail: dict[str, Any]
    ts: float = field(default_factory=time.time)


def redact(text: str) -> tuple[str, list[str]]:
    """Return cleaned text and the labels that were applied. Raw secrets are dropped."""
    labels: list[str] = []
    cleaned = text
    for pattern, replacement in _REDACTIONS:
        cleaned, count = pattern.subn(replacement, cleaned)
        if count:
            label = replacement if isinstance(replacement, str) and replacement.startswith("[") else "[REDACTED_SECRET]"
            labels.extend([label] * count)
    return cleaned, labels


def _spell_ticket(match: re.Match[str]) -> str:
    raw = match.group(0).upper()
    # INC0012345 -> "I N C, 0 0 1, 2 3 4 5" so a TTS engine does not blur the digits.
    prefix, digits = raw[:3], raw[3:]
    groups = [digits[i : i + 3] for i in range(0, len(digits), 3)]
    spelled = [" ".join(prefix)]
    spelled.extend(" ".join(group) for group in groups)
    return ", ".join(spelled)


def normalize_speech(text: str) -> str:
    """Speech contract. Runs even when the model ignores the prompt."""
    spoken = _LINK.sub(r"\1", text or "")
    spoken = _MARKDOWN.sub("", spoken)
    spoken = _INCIDENT_ID.sub(_spell_ticket, spoken)
    spoken = re.sub(r"\s+", " ", spoken).strip()
    if not spoken:
        return "I did not catch that. Could you say it once more?"
    return spoken


class VoiceConversationAgent:
    def __init__(self, model: Any, system_prompt: str, tools: list, handlers: dict) -> None:
        # model.complete(messages, tools) -> ModelTurn
        self.model = model
        self.tools = tools
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_prompt}]
        self.audit: list[AuditEvent] = []
        self.handlers: dict[str, Handler] = dict(handlers)

    def handle_utterance(self, transcript: str) -> SpeechReply:
        cleaned, labels = redact(transcript)
        if labels:
            self.audit.append(AuditEvent("redaction", {"labels": labels}))
        self.messages.append({"role": "user", "content": cleaned})
        tools_used: list[str] = []
        escalated = False

        for _ in range(MAX_STEPS):
            # Audit the outbound transcript, never the original secrets.
            self.audit.append(AuditEvent("model_request", {"tail_role": self.messages[-1]["role"]}))
            turn = self.model.complete(self.messages, self.tools)
            if turn.tool_calls:
                self.messages.append(_assistant_tool_message(turn))
                for call in turn.tool_calls:
                    tools_used.append(call.name)
                    result = self._run_tool(call)
                    if call.name == "escalate" and result.get("transferred"):
                        escalated = True
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result),
                        }
                    )
                continue

            reply = normalize_speech(turn.content or "")
            self.messages.append({"role": "assistant", "content": reply})
            self.audit.append(AuditEvent("speech", {"chars": len(reply), "tools": list(tools_used)}))
            return SpeechReply(reply, tools_used, escalated)

        reply = normalize_speech("I am having trouble finishing that. I will transfer you to a person.")
        self.messages.append({"role": "assistant", "content": reply})
        return SpeechReply(reply, tools_used, escalated=True)

    def _run_tool(self, call: ToolCall) -> dict[str, Any]:
        self.audit.append(AuditEvent("tool_call", {"name": call.name, "arguments": call.arguments}))
        handler = self.handlers.get(call.name)
        if handler is None:
            result = {"ok": False, "error": "unknown_tool"}
        else:
            try:
                result = handler(**call.arguments)
            except TypeError:
                result = {"ok": False, "error": "bad_arguments"}
        self.audit.append(AuditEvent("tool_result", {"name": call.name, "ok": bool(result.get("ok", True))}))
        return result


def _assistant_tool_message(turn: ModelTurn) -> dict[str, Any]:
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
