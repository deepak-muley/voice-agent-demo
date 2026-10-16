"""GPT-Live as the voice in front of the LangGraph desk.

Nothing in chatgpt_live/ is copied here. That folder owns the microphone,
the speaker clock, and the socket. This module is the callback: a delegation
transcript goes into ``LangGraphDesk.handle_utterance``, and the sentence it
returns is what GPT-Live says.

Run from the repo root, with the virtualenv:

  OPENAI_API_KEY=... .venv/bin/python single_agent_langgraph/run.py --voice
"""

from __future__ import annotations

import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _folder in (_ROOT / "single_agent", _ROOT / "chatgpt_live"):
    if str(_folder) not in sys.path:
        sys.path.append(str(_folder))

from graph import LangGraphDesk
from lab import Lab


def on_delegation(desk: LangGraphDesk):
    """One transcript in, one sentence out. The voice model speaks the sentence."""

    def answer(transcript: str) -> str:
        reply = desk.handle_utterance(transcript)
        print(f"agent > {reply.text}")
        if reply.tools_used:
            print(f"tools > {', '.join(reply.tools_used)}")
        return reply.text

    return answer


def check_wire() -> str:
    """Scripted graph, one delegation event, no socket and no microphone."""
    from model import ScriptedChatModel

    desk = LangGraphDesk(ScriptedChatModel())
    lab = Lab(
        "client",
        caller_line="",
        on_client=on_delegation(desk),
    )
    lab.input_transcript = "My VPN will not connect and I have a meeting in twenty minutes."
    event = {
        "type": "session.delegation.created",
        "delegation": {"id": "item_lg", "type": "delegation", "target": "client"},
    }
    sent = lab.handle(event)
    assert len(sent) == 1
    assert sent[0]["type"] == "session.commentary.append"
    assert sent[0]["delegation_id"] == "item_lg"
    assert "full-tunnel" in sent[0]["content"] or "ticket" in sent[0]["content"].lower()
    assert lab.handle(event) == []
    return sent[0]["content"]


def run_voice(verbose: bool = False, play: bool = True, show_calls: bool = False) -> None:
    """Open the microphone. The graph runs when GPT-Live delegates."""
    import os

    from langchain_openai import ChatOpenAI

    from live import run_live
    from voice_agent import TOOLS

    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY.", file=sys.stderr)
        sys.exit(1)
    chat = ChatOpenAI(
        model=os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
        base_url=os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        api_key=api_key,
        temperature=0.2,
        timeout=30,
        max_retries=2,
    ).bind_tools(TOOLS)
    desk = LangGraphDesk(chat, verbose=verbose, show_calls=show_calls)
    print("GPT-Live is the voice. This graph answers when it delegates.")
    run_live(None, verbose=verbose, play=play, mic=True, on_client=on_delegation(desk))
