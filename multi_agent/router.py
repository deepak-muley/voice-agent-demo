"""Router. Demo uses rules. Live asks the model for one of four words.

The rules are the same branches as ``ScriptedModel`` in the single agent, so
the offline call takes the same path.
"""

from __future__ import annotations

import re
from typing import Any

from contracts import ROUTES, CallFacts, caller_agreed
from prompts import ROUTER_PROMPT


class RuleRouter:
    """Closed branches. No model call."""

    def route(self, utterance: str, facts: CallFacts) -> str:
        del facts  # The scripted single agent looks only at the latest line.
        lowered = utterance.lower()
        if "ignore previous" in lowered or "dump all" in lowered:
            return "voice"
        if re.search(r"\binc\d+\b", lowered):
            return "incident"
        if caller_agreed(utterance):
            return "incident"
        if "person" in lowered or "human" in lowered or "transfer" in lowered:
            return "escalation"
        return "knowledge"


class ModelRouter:
    """One completion, no tools. Used by --live."""

    def __init__(self, model: Any) -> None:
        self.model = model

    def route(self, utterance: str, facts: CallFacts) -> str:
        messages = [
            {"role": "system", "content": ROUTER_PROMPT},
            {
                "role": "user",
                "content": f"utterance: {utterance}\ncaller_agreed: {str(facts.caller_agreed).lower()}",
            },
        ]
        turn = self.model.complete(messages, [])
        return parse_route(turn.content)


def parse_route(content: str | None) -> str:
    for word in re.findall(r"[a-z]+", (content or "").lower()):
        if word in ROUTES:
            return word
    return "knowledge"
