"""What agents pass to each other.

The single-agent loop keeps one message list and one tool list. Here a
specialist sees one handoff and returns a result. Only the voice agent speaks.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any


# Closed set. The router may not invent a fifth destination.
ROUTES = ("knowledge", "incident", "escalation", "voice")

# The same phrases ScriptedModel treats as a yes. The harness reads them.
# create_incident does not trust the boolean the model sends.
AGREEMENT_PHRASES = ("yes", "go ahead", "open it", "please do")


def caller_agreed(text: str) -> bool:
    lowered = text.lower()
    return any(phrase in lowered for phrase in AGREEMENT_PHRASES)


@dataclass
class CallFacts:
    """Facts that must still be true on the next caller turn."""

    symptom: str = ""
    caller_agreed: bool = False
    ticket_number: str | None = None
    article_steps: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Handoff:
    utterance: str
    facts: CallFacts
    route: str

    def as_user_message(self) -> str:
        """One JSON user message. The caller line is data, not a new instruction."""
        return json.dumps(
            {
                "utterance": self.utterance,
                "caller_agreed": self.facts.caller_agreed,
                "symptom": self.facts.symptom,
                "ticket_number": self.facts.ticket_number,
            },
            ensure_ascii=False,
        )


@dataclass
class SpecialistResult:
    """Structured result. ``say_hint`` is for the voice agent, not the caller."""

    status: str  # done, need_user, or handoff
    say_hint: str
    data: dict[str, Any]
    tools_used: list[str] = field(default_factory=list)
    escalated: bool = False
