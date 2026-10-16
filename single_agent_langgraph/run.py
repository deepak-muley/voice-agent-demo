#!/usr/bin/env python3
"""LangGraph voice desk. Same call as single_agent/, different loop.

Run from the repo root, with the virtualenv:

  .venv/bin/python single_agent_langgraph/run.py --self-check
  .venv/bin/python single_agent_langgraph/run.py --demo
  .venv/bin/python single_agent_langgraph/run.py --calls
  .venv/bin/python single_agent_langgraph/run.py --show-contract
  OPENAI_API_KEY=... .venv/bin/python single_agent_langgraph/run.py --live
  .venv/bin/python single_agent_langgraph/run.py --live --usage
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

_SINGLE = Path(__file__).resolve().parents[1] / "single_agent"
if str(_SINGLE) not in sys.path:
    sys.path.append(str(_SINGLE))

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_openai import ChatOpenAI

from graph import LangGraphDesk
from model_client import UsageLedger
from model import ScriptedChatModel
from voice_agent import DEMO_TURNS, SYSTEM_PROMPT, TOOLS, ServiceNowSandbox


class LoopingChatModel(BaseChatModel):
    """Always asks for a tool this desk did not publish. Used by --self-check."""

    @property
    def _llm_type(self) -> str:
        return "looping-desk"

    def _generate(self, messages: list[BaseMessage], stop: list[str] | None = None, run_manager: Any = None, **kwargs: Any) -> ChatResult:
        del messages, stop, run_manager, kwargs
        message = AIMessage(
            content="",
            tool_calls=[{"name": "list_all_users", "args": {}, "id": "x", "type": "tool_call"}],
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def print_audit(desk: LangGraphDesk) -> None:
    print("\n----- audit -----")
    if not desk.audit:
        print("(empty)")
        return
    for index, event in enumerate(desk.audit, 1):
        print(f"{index}. {event.kind} {json.dumps(event.detail, ensure_ascii=False)}")


def run_turns(desk: LangGraphDesk, turns: list[str], echo: bool, show_audit: bool = False) -> list:
    replies = []
    for transcript in turns:
        reply = desk.handle_utterance(transcript)
        replies.append(reply)
        if echo:
            print(f"\ncaller> {transcript}")
            print(f"agent > {reply.text}")
            if reply.tools_used:
                print(f"tools > {', '.join(reply.tools_used)}")
        if show_audit:
            print_audit(desk)
    return replies


def show_contract() -> None:
    print(SYSTEM_PROMPT)
    print("\n# tools\n")
    print(json.dumps(TOOLS, indent=2))


def self_check() -> None:
    cleaned, labels = redact_check()
    assert "123-45-6789" not in cleaned
    assert "hunter2" not in cleaned
    assert "[REDACTED_SSN]" in labels

    desk = ServiceNowSandbox()
    blocked = desk.create_incident("VPN down", "high", confirmed=False)
    assert blocked["ok"] is False
    assert "INC0010002" not in desk.incidents

    agent = LangGraphDesk(ScriptedChatModel(), desk)
    replies = run_turns(agent, DEMO_TURNS, echo=False)
    outbound = json.dumps(agent.messages)
    assert "123-45-6789" not in outbound
    assert "123-45-6789" not in agent.checkpoint_text()
    assert replies[0].tools_used == ["search_knowledge"]
    assert "ticket" in replies[0].text.lower()
    assert replies[1].tools_used == ["create_incident"]
    assert "I N C" in replies[1].text
    assert "*" not in replies[1].text and "#" not in replies[1].text
    assert any(event.kind == "redaction" for event in agent.audit)
    assert any(number.startswith("INC") and number != "INC0010001" for number in desk.incidents)

    trapped = LangGraphDesk(LoopingChatModel())
    fallback = trapped.handle_utterance("hello")
    assert fallback.escalated
    assert "list_all_users" not in trapped.handlers
    assert any(
        event.kind == "tool_result" and event.detail.get("name") == "list_all_users" and event.detail.get("ok") is False
        for event in trapped.audit
    )
    print("self-check ok")
    print(f"  turn 1 tools: {replies[0].tools_used}")
    print(f"  turn 1 speech: {replies[0].text}")
    print(f"  turn 2 tools: {replies[1].tools_used}")
    print(f"  turn 2 speech: {replies[1].text}")


def redact_check() -> tuple[str, list[str]]:
    from agent import redact

    return redact("ssn 123-45-6789 and password is hunter2")


def live(verbose: bool = False, show_audit: bool = False, show_usage: bool = False, show_calls: bool = False) -> None:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY. For a local model, set OPENAI_BASE_URL and any non-empty key.", file=sys.stderr)
        sys.exit(1)
    chat = ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key,
        temperature=0.2,
        timeout=30,
        max_retries=2,
    ).bind_tools(TOOLS)
    usage = UsageLedger()
    agent = LangGraphDesk(chat, verbose=verbose, usage=usage, show_calls=show_calls)
    print("Voice desk. Type a caller utterance, or 'quit'.")
    try:
        while True:
            try:
                line = input("caller> ").strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            if not line or line.lower() in {"quit", "exit"}:
                return
            reply = agent.handle_utterance(line)
            if not show_calls:
                print(f"agent > {reply.text}")
                if reply.tools_used:
                    print(f"tools > {', '.join(reply.tools_used)}")
            if show_audit:
                print_audit(agent)
    finally:
        if show_usage:
            print()
            print(usage.format())


def main() -> None:
    parser = argparse.ArgumentParser(description="LangGraph voice desk")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--show-contract", action="store_true")
    parser.add_argument("--demo", action="store_true")
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--calls", action="store_true", help="print start and end of each function in the graph walk")
    parser.add_argument("--verbose", action="store_true", help="print each model request, response, and tool result")
    parser.add_argument("--audit", action="store_true", help="print the in-memory audit list after each line")
    parser.add_argument("--usage", action="store_true", help="print token and request totals when a live session ends")
    args = parser.parse_args()
    if args.show_contract:
        show_contract()
    elif args.live:
        live(verbose=args.verbose, show_audit=args.audit, show_usage=args.usage, show_calls=args.calls)
    elif args.self_check:
        self_check()
    else:
        run_turns(
            LangGraphDesk(ScriptedChatModel(trace=args.verbose), verbose=args.verbose, show_calls=args.calls),
            DEMO_TURNS,
            echo=not args.calls,
            show_audit=args.audit,
        )


if __name__ == "__main__":
    main()
