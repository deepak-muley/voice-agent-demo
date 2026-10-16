"""The single-agent loop as a LangGraph.

``VoiceConversationAgent.handle_utterance`` is a ``for`` of at most four steps.
This file is that ``for``, drawn as nodes. The prompt, the sandbox, the
redaction, and the speech guard stay in ``single_agent/``.

Redaction runs before ``invoke``. The checkpointer writes the input before
the first node, so a secret inside the input would be stored.
"""

from __future__ import annotations

import json
import operator
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal

_SINGLE = Path(__file__).resolve().parents[1] / "single_agent"
if str(_SINGLE) not in sys.path:
    sys.path.append(str(_SINGLE))

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from agent import MAX_STEPS, AuditEvent, SpeechReply, normalize_speech, redact
from model_client import UsageLedger, trace_json
from voice_agent import SYSTEM_PROMPT, ServiceNowSandbox

from model import ScriptedChatModel, message_text, to_openai_messages


class DeskState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    steps: int
    tools_used: list[str]
    escalated: bool
    audit: Annotated[list[dict[str, Any]], operator.add]


class _Note:
    detail: str = ""


class _CallLog:
    """Indented start and end lines for one caller utterance. Off unless ``--calls``."""

    def __init__(self, enabled: bool) -> None:
        self.enabled = enabled
        self.depth = 0
        self.walk: list[str] = []

    @contextmanager
    def call(self, name: str, detail: str = "") -> Iterator[_Note]:
        note = _Note()
        if not self.enabled:
            yield note
            return
        suffix = f"  {detail}" if detail else ""
        print(f"{'  ' * self.depth}start {name}{suffix}")
        self.depth += 1
        try:
            yield note
        finally:
            self.depth -= 1
            end = f"{'  ' * self.depth}end {name}"
            if note.detail:
                end += f"  {note.detail}"
            print(end)

    def mark(self, step: str) -> None:
        if self.enabled:
            self.walk.append(step)

    def show_walk(self) -> None:
        if self.enabled and self.walk:
            print("walk: " + " → ".join(self.walk))


def build_graph(
    model: Any,
    handlers: dict[str, Any],
    verbose: bool = False,
    trace_model: bool = False,
    usage: UsageLedger | None = None,
    calls: _CallLog | None = None,
):
    """Compile the tool loop. ``model.invoke`` is ``ScriptedChatModel`` or ``ChatOpenAI``."""

    log = calls or _CallLog(False)

    def model_node(state: DeskState) -> dict[str, Any]:
        log.mark("model")
        with log.call("model") as note:
            if trace_model:
                trace_json("sent to model", {"messages": to_openai_messages(state["messages"])})
            started = time.perf_counter()
            with log.call("model.invoke") as invoked:
                response = model.invoke(state["messages"])
                names = [call["name"] for call in (getattr(response, "tool_calls", None) or [])]
                invoked.detail = "tool_calls=" + ",".join(names) if names else "sentence"
            if usage is not None:
                usage.record_langchain(response, (time.perf_counter() - started) * 1000)
            if trace_model:
                trace_json("received from model", to_openai_messages([response])[0])
            steps = state["steps"] + 1
            note.detail = f"steps={steps}"
            return {
                "messages": [response],
                "steps": steps,
                "audit": [_event("model_request", {"tail_role": _role(state["messages"][-1])})],
            }

    def tools_node(state: DeskState) -> dict[str, Any]:
        last = state["messages"][-1]
        used = list(state["tools_used"])
        escalated = state["escalated"]
        messages: list[ToolMessage] = []
        audit: list[dict[str, Any]] = []
        names: list[str] = []
        with log.call("tools") as note:
            for call in last.tool_calls:
                name = call["name"]
                arguments = call.get("args") or {}
                names.append(name)
                used.append(name)
                audit.append(_event("tool_call", {"name": name, "arguments": arguments}))
                with log.call(name) as tool_note:
                    result = _run_handler(handlers, name, arguments)
                    tool_note.detail = "ok" if result.get("ok", True) else str(result.get("error") or "error")
                if name == "escalate" and result.get("transferred"):
                    escalated = True
                audit.append(_event("tool_result", {"name": name, "ok": bool(result.get("ok", True))}))
                tool_message = ToolMessage(content=json.dumps(result), tool_call_id=call["id"])
                messages.append(tool_message)
                if verbose:
                    trace_json(
                        "tool result",
                        {"role": "tool", "tool_call_id": call["id"], "content": tool_message.content},
                    )
            note.detail = ",".join(names)
        log.mark("tools(" + ",".join(names) + ")")
        return {"messages": messages, "tools_used": used, "escalated": escalated, "audit": audit}

    def speak_node(state: DeskState) -> dict[str, Any]:
        log.mark("speak")
        with log.call("speak") as note:
            last = state["messages"][-1]
            with log.call("normalize_speech") as speech:
                reply = normalize_speech(message_text(last))
                speech.detail = _clip(reply)
            note.detail = _clip(reply)
            return {
                "messages": [AIMessage(content=reply, id=last.id)],
                "audit": [_event("speech", {"chars": len(reply), "tools": list(state["tools_used"])})],
            }

    def transfer_node(state: DeskState) -> dict[str, Any]:
        log.mark("transfer")
        with log.call("transfer") as note:
            with log.call("normalize_speech") as speech:
                reply = normalize_speech("I am having trouble finishing that. I will transfer you to a person.")
                speech.detail = _clip(reply)
            note.detail = _clip(reply)
            return {
                "messages": [AIMessage(content=reply)],
                "escalated": True,
                "audit": [_event("speech", {"chars": len(reply), "tools": list(state["tools_used"])})],
            }

    def after_model(state: DeskState) -> Literal["tools", "speak"]:
        with log.call("after_model") as note:
            last = state["messages"][-1]
            calls = getattr(last, "tool_calls", None) or []
            if calls:
                names = ",".join(call["name"] for call in calls)
                note.detail = f"next=tools  tool_calls={names}"
                log.mark("after_model→tools")
                return "tools"
            note.detail = "next=speak  sentence"
            log.mark("after_model→speak")
            return "speak"

    def after_tools(state: DeskState) -> Literal["model", "transfer"]:
        # The model already ran this step. At the cap, do not call it again.
        with log.call("after_tools") as note:
            if state["steps"] >= MAX_STEPS:
                note.detail = f"next=transfer  steps={state['steps']}"
                log.mark("after_tools→transfer")
                return "transfer"
            note.detail = f"next=model  steps={state['steps']}"
            log.mark("after_tools→model")
            return "model"

    builder = StateGraph(DeskState)
    builder.add_node("model", model_node)
    builder.add_node("tools", tools_node)
    builder.add_node("speak", speak_node)
    builder.add_node("transfer", transfer_node)
    builder.add_edge(START, "model")
    builder.add_conditional_edges("model", after_model)
    builder.add_conditional_edges("tools", after_tools)
    builder.add_edge("speak", END)
    builder.add_edge("transfer", END)
    return builder.compile(checkpointer=InMemorySaver())


class LangGraphDesk:
    """One call. ``thread_id`` is the checkpoint key, the way ``messages`` is on the other desk."""

    def __init__(
        self,
        model: Any,
        desk: ServiceNowSandbox | None = None,
        verbose: bool = False,
        usage: UsageLedger | None = None,
        show_calls: bool = False,
    ) -> None:
        self.desk = desk or ServiceNowSandbox()
        self.usage = usage
        self._calls = _CallLog(show_calls)
        self.handlers = {
            "search_knowledge": self.desk.search_knowledge,
            "get_incident": self.desk.get_incident,
            "create_incident": self.desk.create_incident,
            "escalate": self.desk.escalate,
        }
        self.graph = build_graph(
            model,
            self.handlers,
            verbose=verbose,
            trace_model=verbose and not isinstance(model, ScriptedChatModel),
            usage=usage,
            calls=self._calls,
        )
        self.config = {"configurable": {"thread_id": "call"}}
        self._seeded = False

    def handle_utterance(self, transcript: str) -> SpeechReply:
        self._calls.walk = []
        if self._calls.enabled:
            print()
        with self._calls.call("handle_utterance", _clip(transcript)) as turn:
            self._calls.mark("user")
            with self._calls.call("redact") as redacted:
                cleaned, labels = redact(transcript)
                redacted.detail = ",".join(labels) if labels else "clean"
            messages: list[BaseMessage] = []
            if not self._seeded:
                messages.append(SystemMessage(content=SYSTEM_PROMPT))
                self._seeded = True
            messages.append(HumanMessage(content=cleaned))
            incoming: dict[str, Any] = {
                "messages": messages,
                "steps": 0,
                "tools_used": [],
                "escalated": False,
            }
            if labels:
                incoming["audit"] = [_event("redaction", {"labels": labels})]
            with self._calls.call("graph.invoke"):
                self._calls.mark("START")
                out = self.graph.invoke(incoming, self.config)
                self._calls.mark("END")
            reply = SpeechReply(_last_speech(out["messages"]), list(out["tools_used"]), bool(out["escalated"]))
            self._calls.mark("user")
            turn.detail = _clip(reply.text)
        self._calls.show_walk()
        return reply

    @property
    def audit(self) -> list[AuditEvent]:
        values = self.graph.get_state(self.config).values
        return [AuditEvent(event["kind"], event["detail"], event["ts"]) for event in values.get("audit") or []]

    @property
    def messages(self) -> list[dict[str, Any]]:
        values = self.graph.get_state(self.config).values
        return to_openai_messages(list(values.get("messages") or []))

    def checkpoint_text(self) -> str:
        """Every saved snapshot, so a self-check can prove a secret never landed."""
        chunks: list[str] = []
        for snapshot in self.graph.get_state_history(self.config):
            chunks.append(json.dumps(snapshot.values, default=str))
        return "\n".join(chunks)


def _run_handler(handlers: dict[str, Any], name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    handler = handlers.get(name)
    if handler is None:
        return {"ok": False, "error": "unknown_tool"}
    try:
        result = handler(**arguments)
    except TypeError:
        return {"ok": False, "error": "bad_arguments"}
    return result


def _event(kind: str, detail: dict[str, Any]) -> dict[str, Any]:
    return {"kind": kind, "detail": detail, "ts": time.time()}


def _role(message: BaseMessage) -> str:
    if isinstance(message, SystemMessage):
        return "system"
    if isinstance(message, HumanMessage):
        return "user"
    if isinstance(message, ToolMessage):
        return "tool"
    return "assistant"


def _clip(text: str, limit: int = 88) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 3] + "..."


def _last_speech(messages: list[BaseMessage]) -> str:
    for message in reversed(messages):
        if isinstance(message, AIMessage) and not message.tool_calls:
            return message_text(message)
    return ""
