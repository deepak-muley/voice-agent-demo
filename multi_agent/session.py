"""One caller turn: redact, route, run one specialist, then speak.

This is the piece a graph library would replace with nodes and edges. The
specialists, the prompts, and the guards would still be here.
"""

from __future__ import annotations

from typing import Any, Callable

from contracts import CallFacts, Handoff, SpecialistResult, caller_agreed
from prompts import ESCALATION_PROMPT, INCIDENT_PROMPT, KNOWLEDGE_PROMPT
from router import RuleRouter
from scripted import ScriptedRoleModel
from single_imports import TOOLS, AuditEvent, ServiceNowSandbox, SpeechReply, redact, trace_json
from specialist import Specialist
from voice import ScriptedVoice


Handler = Callable  # (**arguments) -> dict. Left unparameterized so Python 3.7 can load it.

_PROMPTS = {
    "knowledge": KNOWLEDGE_PROMPT,
    "incident": INCIDENT_PROMPT,
    "escalation": ESCALATION_PROMPT,
}

_TOOL_NAMES = {
    "knowledge": ["search_knowledge"],
    "incident": ["get_incident", "create_incident"],
    "escalation": ["escalate"],
}


def tool_schemas(names: list[str]) -> list[dict[str, Any]]:
    wanted = set(names)
    return [tool for tool in TOOLS if tool["function"]["name"] in wanted]


def handlers_for(desk: ServiceNowSandbox, names: list[str]) -> dict[str, Handler]:
    table: dict[str, Handler] = {
        "search_knowledge": desk.search_knowledge,
        "get_incident": desk.get_incident,
        "create_incident": desk.create_incident,
        "escalate": desk.escalate,
    }
    return {name: table[name] for name in names}


def wire_specialists(model_for: Callable[[str], Any], desk: ServiceNowSandbox, verbose: bool = False) -> dict[str, Specialist]:
    specialists: dict[str, Specialist] = {}
    for name, tool_names in _TOOL_NAMES.items():
        specialists[name] = Specialist(
            model_for(name),
            name,
            _PROMPTS[name],
            tool_schemas(tool_names),
            handlers_for(desk, tool_names),
            verbose=verbose,
        )
    return specialists


class DeskSession:
    def __init__(
        self,
        router: Any,
        specialists: dict[str, Specialist],
        voice: Any,
        desk: ServiceNowSandbox,
        verbose: bool = False,
    ) -> None:
        self.router = router
        self.specialists = specialists
        self.voice = voice
        self.desk = desk
        self.verbose = verbose
        self.audit: list[AuditEvent] = []
        self.messages: list[dict[str, Any]] = []
        self.facts = CallFacts()
        self.last_route = "voice"

    def handle_utterance(self, transcript: str) -> SpeechReply:
        cleaned, labels = redact(transcript)
        if labels:
            self.audit.append(AuditEvent("redaction", {"labels": labels}))
        self.facts.caller_agreed = caller_agreed(cleaned)
        self.messages.append({"role": "user", "content": cleaned})

        route = self.router.route(cleaned, self.facts)
        if route not in self.specialists and route != "voice":
            route = "knowledge"
        self.last_route = route
        self.audit.append(AuditEvent("route", {"route": route}))
        if self.verbose:
            trace_json("route", {"route": route, "utterance": cleaned, "facts": self.facts.to_dict()})

        if route == "voice":
            text = self.voice.speak(cleaned, None)
            tools_used: list[str] = []
            escalated = False
        else:
            result = self.specialists[route].run(Handoff(cleaned, self.facts, route), self.audit)
            self._absorb(cleaned, result)
            text = self.voice.speak(cleaned, result)
            tools_used = list(result.tools_used)
            escalated = result.escalated

        self.messages.append({"role": "assistant", "content": text})
        self.audit.append(AuditEvent("speech", {"chars": len(text), "tools": tools_used}))
        return SpeechReply(text, tools_used, escalated)

    def _absorb(self, utterance: str, result: SpecialistResult) -> None:
        if not self.facts.symptom and result.tools_used:
            self.facts.symptom = utterance
        articles = result.data.get("articles") or []
        if articles:
            self.facts.article_steps = articles[0].get("steps")
        incident = result.data.get("incident")
        if isinstance(incident, dict) and incident.get("number"):
            self.facts.ticket_number = incident["number"]


def build_scripted(verbose: bool = False, desk: ServiceNowSandbox | None = None) -> DeskSession:
    if desk is None:
        desk = ServiceNowSandbox()
    return DeskSession(
        router=RuleRouter(),
        specialists=wire_specialists(lambda name: ScriptedRoleModel(name, verbose=verbose), desk, verbose),
        voice=ScriptedVoice(),
        desk=desk,
        verbose=verbose,
    )
