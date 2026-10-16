"""The only agent that produces caller-facing speech.

The scripted voice copies the sentences from ``ScriptedModel``, so the demo
matches the single agent. ``--live`` uses ``ModelVoice`` and ``VOICE_PROMPT``.
"""

from __future__ import annotations

import json
from typing import Any

from contracts import SpecialistResult
from prompts import VOICE_PROMPT
from single_imports import normalize_speech


class ScriptedVoice:
    def speak(self, utterance: str, result: SpecialistResult | None) -> str:
        if result is None:
            lowered = utterance.lower()
            if "ignore previous" in lowered or "dump all" in lowered:
                text = "I can help with your own ticket or a new issue. What is failing?"
            else:
                text = "I can help you. What issue are you experiencing?"
            return normalize_speech(text)

        if result.escalated and not result.data.get("transferred"):
            return normalize_speech(result.say_hint)

        if result.data:
            return normalize_speech(_from_tool_payload(result.data))

        return normalize_speech(result.say_hint or "I did not catch that. Could you say it once more?")


class ModelVoice:
    """Keeps the caller-facing transcript. Specialists do not write into it."""

    def __init__(self, model: Any) -> None:
        self.model = model
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": VOICE_PROMPT}]

    def speak(self, utterance: str, result: SpecialistResult | None) -> str:
        if result is None:
            content = utterance
        else:
            content = utterance + "\n\nDesk result:\n" + json.dumps(
                {"say_hint": result.say_hint, "data": result.data},
                ensure_ascii=False,
            )
        self.messages.append({"role": "user", "content": content})
        turn = self.model.complete(self.messages, [])
        text = normalize_speech(turn.content or "")
        self.messages.append({"role": "assistant", "content": text})
        return text


def _from_tool_payload(payload: dict[str, Any]) -> str:
    """The tool-result branch of ScriptedModel._reply, word for word."""
    if "articles" in payload:
        if payload["articles"]:
            step = payload["articles"][0]["steps"]
            return (
                f"You are tight on time. {step} "
                "Want me to open an urgent ticket while you try that?"
            )
        return "I could not find a known fix. Want me to open a ticket for the desk?"
    if payload.get("incident"):
        number = payload["incident"]["number"]
        urgency = payload["incident"]["urgency"]
        return (
            f"I opened {number} as {urgency} urgency. "
            "The desk has it, and you can try the VPN steps while you wait."
        )
    if payload.get("error") == "caller_has_not_confirmed":
        return "I can open that ticket. Should I go ahead?"
    if payload.get("transferred"):
        return "I am transferring you to the service desk now. Please stay on the line."
    return "I could not look that up. Can you repeat the ticket number?"
