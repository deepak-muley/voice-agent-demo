#!/usr/bin/env python3
"""Multi-agent desk. Same call as the single agent, split by role.

Run from the repo root:
  python3 multi_agent/run.py --self-check
  python3 multi_agent/run.py --demo
  OPENAI_API_KEY=... python3 multi_agent/run.py --live
  python3 multi_agent/run.py --demo --verbose
  python3 multi_agent/run.py --demo --audit
  python3 multi_agent/run.py --live --usage
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from contracts import Handoff
from router import ModelRouter
from session import DeskSession, build_scripted, wire_specialists
from single_imports import (
    DEMO_TURNS,
    AuditEvent,
    ModelTurn,
    OpenAICompatibleClient,
    ScriptedModel,
    ServiceNowSandbox,
    SpeechReply,
    ToolCall,
    build_agent,
    run_turns,
)
from specialist import Specialist
from voice import ModelVoice


def print_audit(audit: list[AuditEvent]) -> None:
    print("\n----- audit -----")
    if not audit:
        print("(empty)")
        return
    for index, event in enumerate(audit, 1):
        print(f"{index}. {event.kind} {json.dumps(event.detail, ensure_ascii=False)}")


def run_session(session: DeskSession, turns: list[str], echo: bool, show_audit: bool = False) -> list[SpeechReply]:
    replies: list[SpeechReply] = []
    for transcript in turns:
        reply = session.handle_utterance(transcript)
        replies.append(reply)
        if echo:
            print(f"\ncaller> {transcript}")
            print(f"route > {session.last_route}")
            print(f"agent > {reply.text}")
            if reply.tools_used:
                print(f"tools > {', '.join(reply.tools_used)}")
        if show_audit:
            print_audit(session.audit)
    return replies


def self_check() -> None:
    single = run_turns(build_agent(ScriptedModel()), DEMO_TURNS, echo=False)
    session = build_scripted()
    multi = run_session(session, DEMO_TURNS, echo=False)

    assert [reply.text for reply in single] == [reply.text for reply in multi]
    assert [reply.tools_used for reply in single] == [reply.tools_used for reply in multi]
    assert "123-45-6789" not in json.dumps(session.messages)
    assert [event.detail["route"] for event in session.audit if event.kind == "route"] == ["knowledge", "incident"]
    assert any(event.kind == "redaction" for event in session.audit)
    assert any(number.startswith("INC") and number != "INC0010001" for number in session.desk.incidents)
    assert "create_incident" not in session.specialists["knowledge"].handlers
    assert "search_knowledge" not in session.specialists["incident"].handlers

    # A model that only calls an unpublished tool cannot loop, and cannot leave this specialist.
    class LoopingModel:
        def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
            del messages, tools
            return ModelTurn(None, [ToolCall("x", "list_all_users", {})])

    trapped_desk = ServiceNowSandbox()
    trapped = build_scripted(desk=trapped_desk)
    trapped.specialists["knowledge"] = Specialist(
        LoopingModel(),
        "knowledge",
        trapped.specialists["knowledge"].system_prompt,
        trapped.specialists["knowledge"].tools,
        trapped.specialists["knowledge"].handlers,
    )
    fallback = trapped.handle_utterance("my vpn is down")
    assert fallback.escalated
    assert "list_all_users" not in trapped.specialists["knowledge"].handlers
    assert any(
        event.kind == "tool_result" and event.detail.get("name") == "list_all_users" and event.detail.get("ok") is False
        for event in trapped.audit
    )

    # The incident specialist ignores confirmed=true unless this turn agreed.
    class EagerCreate:
        def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
            del tools
            if messages[-1]["role"] == "tool":
                return ModelTurn(content="I opened it.", tool_calls=[])
            return ModelTurn(
                None,
                [
                    ToolCall(
                        "c",
                        "create_incident",
                        {"short_description": "VPN down", "urgency": "high", "confirmed": True},
                    )
                ],
            )

    guarded_desk = ServiceNowSandbox()
    guarded = build_scripted(desk=guarded_desk)
    guarded.specialists["incident"] = Specialist(
        EagerCreate(),
        "incident",
        guarded.specialists["incident"].system_prompt,
        guarded.specialists["incident"].tools,
        guarded.specialists["incident"].handlers,
    )
    guarded.handle_utterance("status of INC0010001")
    assert "INC0010002" not in guarded_desk.incidents

    # Knowledge cannot open a ticket even when its model asks to.
    class CreateFromKnowledge:
        def complete(self, messages: list[dict], tools: list[dict]) -> ModelTurn:
            del tools
            if messages[-1]["role"] == "tool":
                return ModelTurn(content="no", tool_calls=[])
            return ModelTurn(
                None,
                [
                    ToolCall(
                        "c",
                        "create_incident",
                        {"short_description": "VPN down", "urgency": "high", "confirmed": True},
                    )
                ],
            )

    blocked_desk = ServiceNowSandbox()
    blocked = build_scripted(desk=blocked_desk)
    blocked.specialists["knowledge"] = Specialist(
        CreateFromKnowledge(),
        "knowledge",
        blocked.specialists["knowledge"].system_prompt,
        blocked.specialists["knowledge"].tools,
        blocked.specialists["knowledge"].handlers,
    )
    blocked.handle_utterance("my vpn will not connect")
    assert "INC0010002" not in blocked_desk.incidents
    assert any(
        event.kind == "tool_result" and event.detail.get("name") == "create_incident" and event.detail.get("ok") is False
        for event in blocked.audit
    )

    # Handoff JSON is what the specialist sees, not the raw secret.
    sample = Handoff("ssn was removed", session.facts, "knowledge")
    assert "utterance" in sample.as_user_message()

    print("self-check ok")
    print(f"  same speech as single agent: {multi[0].text == single[0].text and multi[1].text == single[1].text}")
    print(f"  turn 1 route: knowledge  tools: {multi[0].tools_used}")
    print(f"  turn 2 route: incident   tools: {multi[1].tools_used}")


def live(verbose: bool = False, show_audit: bool = False, show_usage: bool = False) -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY. For a local model, set OPENAI_BASE_URL and any non-empty key.", file=sys.stderr)
        sys.exit(1)
    client = OpenAICompatibleClient(
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key,
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        verbose=verbose,
    )
    desk = ServiceNowSandbox()
    session = DeskSession(
        router=ModelRouter(client),
        specialists=wire_specialists(lambda name: client, desk, verbose),
        voice=ModelVoice(client),
        desk=desk,
        verbose=verbose,
    )
    print("Multi-agent voice desk. Type a caller utterance, or 'quit'.")
    try:
        while True:
            try:
                line = input("caller> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line or line.lower() in {"quit", "exit"}:
                return
            reply = session.handle_utterance(line)
            print(f"route > {session.last_route}")
            print(f"agent > {reply.text}")
            if reply.tools_used:
                print(f"tools > {', '.join(reply.tools_used)}")
            if show_audit:
                print_audit(session.audit)
    finally:
        if show_usage:
            print()
            print(client.usage.format())


def main() -> None:
    parser = argparse.ArgumentParser(description="Multi-agent voice desk")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--verbose", action="store_true", help="print each model request, response, and tool result")
    parser.add_argument("--audit", action="store_true", help="print the in-memory audit list after each line")
    parser.add_argument("--usage", action="store_true", help="print token and request totals when a live session ends")
    args = parser.parse_args()
    if args.live:
        live(verbose=args.verbose, show_audit=args.audit, show_usage=args.usage)
    elif args.self_check:
        self_check()
    else:
        run_session(build_scripted(verbose=args.verbose), DEMO_TURNS, echo=True, show_audit=args.audit)


if __name__ == "__main__":
    main()
