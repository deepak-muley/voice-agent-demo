"""The application side of one GPT-Live session.

The socket delivers events. This module decides what to send back. Two
delegation modes are both handled here, because they are the two places a
later integration will attach:

- `client`: GPT-Live asks this program for help. `on_client_delegation` is
  where a desk agent will run. The reply is `session.commentary.append`.
- `responses`: GPT-Live asks a Responses model, and that model asks for a
  function. `on_function_call` is where a plugin will run. The reply is a
  `function_call_output` plus `response.create`.

`ScriptedServer` plays the server events for those two paths so the loop
runs with no API key. It speaks the commentary text back as a transcript.
A live model paraphrases that text and also sends audio.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from contract import CALLER_LINE, session_start


# Same VPN article the desk sandbox returns, as a sentence the voice model can say.
VPN_FIX = (
    "Known fix: quit the VPN client, reconnect to office wifi, then open the client "
    "and pick the full-tunnel profile. No ticket has been opened."
)


def desk_commentary(text: str) -> str:
    """Stand-in used when no agent callback was passed."""
    if "vpn" in text.lower():
        return VPN_FIX
    return "No known fix was found. Ask what is failing. No ticket has been opened."


def search_knowledge(query: str) -> dict[str, Any]:
    """Stand-in for one plugin. A later change calls the desk handler here."""
    if "vpn" in query.lower():
        return {
            "ok": True,
            "articles": [
                {
                    "id": "KB001",
                    "title": "VPN client will not connect",
                    "steps": "Quit the VPN client, reconnect to office wifi, then open the client and pick the full-tunnel profile.",
                }
            ],
        }
    return {"ok": True, "articles": []}


@dataclass
class Lab:
    """State for one session, and the events this program sends in response."""

    mode: str
    caller_line: str = CALLER_LINE
    input_transcript: str = ""
    output_transcript: str = ""
    delegation_ids: list[str] = field(default_factory=list)
    answered: set[str] = field(default_factory=set)
    commentary_sent: bool = False
    tool_result_sent: bool = False
    spoke_after_result: bool = False
    on_client: Callable[[str], str] | None = None
    _seq: int = 0

    def session_start(self) -> dict[str, Any]:
        return session_start(self.mode, self.caller_line or None)

    def heard(self) -> str:
        """What the caller asked. A delegation event does not carry this text."""
        spoken = self.input_transcript.strip()
        if spoken:
            return spoken
        return self.caller_line

    def handle(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "session.input_transcript.delta":
            self.input_transcript += event.get("delta") or ""
            return []
        if kind == "session.output_transcript.delta":
            self.output_transcript += event.get("delta") or ""
            if self.commentary_sent or self.tool_result_sent:
                self.spoke_after_result = True
            return []
        if kind == "session.delegation.created":
            return self._on_delegation(event)
        if kind == "response.event":
            return self._on_response_event(event)
        return []

    def _on_delegation(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        delegation = event.get("delegation") or {}
        delegation_id = delegation.get("id")
        target = delegation.get("target")
        if not delegation_id or delegation_id in self.answered:
            return []
        self.delegation_ids.append(delegation_id)
        if target == "client" and self.mode == "client":
            self.answered.add(delegation_id)
            self.commentary_sent = True
            return [
                {
                    "type": "session.commentary.append",
                    "event_id": self._next_id("result"),
                    "delegation_id": delegation_id,
                    "content": self._commentary(),
                }
            ]
        return []

    def _commentary(self) -> str:
        heard = self.heard()
        if self.on_client is None:
            return desk_commentary(heard)
        try:
            text = self.on_client(heard).strip()
        except Exception as exc:
            print(f"agent failed: {exc}", file=sys.stderr)
            return "I could not complete that lookup. Can you say it once more?"
        return text or "I could not look that up. What is failing?"

    def _on_response_event(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        inner = event.get("event") or {}
        if inner.get("type") != "response.output_item.done":
            return []
        item = inner.get("item") or {}
        if item.get("type") != "function_call":
            return []
        call_id = item.get("call_id")
        if not call_id or call_id in self.answered:
            return []
        self.answered.add(call_id)
        self.tool_result_sent = True
        return [
            {
                "type": "response.item.create",
                "event_id": self._next_id("tool_result"),
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": self._tool_output(item.get("name") or "", item.get("arguments") or "{}"),
                },
            },
            {"type": "response.create", "event_id": self._next_id("continue")},
        ]

    def _tool_output(self, name: str, arguments: str) -> str:
        if name != "search_knowledge":
            return json.dumps({"ok": False, "error": "unknown_tool", "name": name})
        try:
            payload = json.loads(arguments) if arguments else {}
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "error": "invalid_arguments"})
        if not isinstance(payload, dict):
            return json.dumps({"ok": False, "error": "invalid_arguments"})
        return json.dumps(search_knowledge(str(payload.get("query") or "")))

    def _next_id(self, prefix: str) -> str:
        self._seq += 1
        return f"{prefix}_{self._seq}"


class ScriptedServer:
    """Server events for one scripted call. Audio bytes are left out."""

    def __init__(self, mode: str) -> None:
        self.mode = mode
        self.closed = False
        self._opened = False

    def respond(self, event: dict[str, Any]) -> list[dict[str, Any]]:
        kind = event.get("type")
        if kind == "session.start" and not self._opened:
            self._opened = True
            return self._open()
        if kind == "session.commentary.append":
            return [
                {
                    "type": "session.commentary.appended",
                    "event_id": "evt_commentary",
                    "client_event_id": event.get("event_id"),
                    "delegation_id": event.get("delegation_id"),
                },
                {
                    "type": "session.output_transcript.delta",
                    "event_id": "evt_speech",
                    "delta": event.get("content") or "",
                    "start_ms": 2000,
                    "end_ms": 4000,
                },
            ]
        if kind == "response.create":
            return [
                {
                    "type": "response.event",
                    "event_id": "evt_backend_text",
                    "delegation_id": "item_resp_1",
                    "event": {
                        "type": "response.output_text.delta",
                        "delta": "Quit the VPN client, reconnect to office wifi, then open the full-tunnel profile.",
                    },
                },
                {
                    "type": "session.output_transcript.delta",
                    "event_id": "evt_speech",
                    "delta": "Quit the VPN client, reconnect to office wifi, then open the full-tunnel profile.",
                    "start_ms": 2000,
                    "end_ms": 4200,
                },
            ]
        if kind == "session.close":
            self.closed = True
            return [
                {
                    "type": "session.closed",
                    "event_id": "evt_closed",
                    "client_event_id": event.get("event_id"),
                    "reason": "close_requested",
                    "usage": {"seconds": 4},
                    "session": {"id": "sess_lab", "model": "gpt-live-1", "status": "active"},
                }
            ]
        return []

    def _open(self) -> list[dict[str, Any]]:
        started: dict[str, Any] = {
            "type": "session.started",
            "event_id": "evt_started",
            "client_event_id": "event_start",
            "session": {
                "id": "sess_lab",
                "model": "gpt-live-1",
                "status": "active",
                "delegation": {"type": self.mode},
            },
        }
        transcript = [
            {
                "type": "session.input_transcript.delta",
                "event_id": "evt_in_1",
                "delta": "My VPN will not connect",
                "start_ms": 0,
                "end_ms": 900,
            },
            {
                "type": "session.input_transcript.delta",
                "event_id": "evt_in_2",
                "delta": " and I have a meeting in twenty minutes.",
                "start_ms": 900,
                "end_ms": 1800,
            },
        ]
        if self.mode == "client":
            delegation: dict[str, Any] = {
                "type": "session.delegation.created",
                "event_id": "evt_del",
                "offset_ms": 1800,
                "delegation": {"id": "item_lab_1", "type": "delegation", "target": "client"},
            }
            return [started, *transcript, delegation]
        function_call = {
            "type": "response.event",
            "event_id": "evt_fn",
            "delegation_id": "item_resp_1",
            "event": {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_kb",
                    "name": "search_knowledge",
                    "arguments": json.dumps({"query": "vpn will not connect"}),
                },
            },
        }
        delegation = {
            "type": "session.delegation.created",
            "event_id": "evt_del",
            "offset_ms": 1800,
            "delegation": {"id": "item_resp_1", "type": "delegation", "target": "responses"},
        }
        return [started, *transcript, delegation, function_call]


def exchange(lab: Lab, server: ScriptedServer) -> list[tuple[str, dict[str, Any]]]:
    """Play client and server events until the session closes. Returns the log."""
    log: list[tuple[str, dict[str, Any]]] = []
    client_q = [lab.session_start()]
    server_q: list[dict[str, Any]] = []
    while client_q or server_q:
        if client_q:
            event = client_q.pop(0)
            log.append(("client", event))
            server_q.extend(server.respond(event))
        else:
            event = server_q.pop(0)
            log.append(("server", event))
            client_q.extend(lab.handle(event))
    if not server.closed:
        close = {"type": "session.close", "event_id": "event_close"}
        log.append(("client", close))
        for event in server.respond(close):
            log.append(("server", event))
            lab.handle(event)
    return log
