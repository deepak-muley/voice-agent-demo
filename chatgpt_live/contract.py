"""Session configuration for a GPT-Live call.

GPT-Live (`gpt-live-1`) is a speech model on a long-lived WebSocket. This
module builds the `session.start` event and the one audio event a microphone
will send later. It does not open a socket.

Docs: https://developers.openai.com/api/docs/guides/voice-websockets
"""

from __future__ import annotations

import base64
import os
from typing import Any


LIVE_URL = "wss://api.openai.com/v1/live/sessions"
LIVE_MODEL = "gpt-live-1"
LIVE_VOICE = "marin"
SAMPLE_RATE = 24000

# The websocket quickstart names this Responses model for delegated work.
# The delegation guide also mentions gpt-6-luna. Confirm the current name
# before a live plugin session; the scripted plugin trace does not call it.
BACKEND_MODEL = "gpt-5.6-luna"

CALLER_LINE = "My VPN will not connect and I have a meeting in twenty minutes."

# Spoken style and when to hand work off. Ticket rules stay with the backend.
INSTRUCTIONS = """\
You are the voice on an IT service desk. A phone caller hears every word you speak. One or two short sentences, then stop. No markdown, no lists, no ticket ids you were not given.

Delegation policy:
Backend tools:
- Knowledge: look up a known fix for an IT symptom.
- Tickets: read one incident, or open one after the caller has agreed.

Delegate to the backend when:
- The caller describes something that is broken.
- The caller asks about a ticket, or agrees to open one.
- A correction changes a request already in progress.

Do not delegate to the backend when:
- The caller greets you or asks you to repeat something already said.
- You need one short clarification before you know what failed.

Delegate before you suggest a fix or a ticket id.
Do not guess a fix or a ticket id while you wait.
"""

# Flat function schema. GPT-Live's Responses backend wants name at the top.
# single_agent/voice_agent.py nests the same idea under "function" for chat completions.
SEARCH_KNOWLEDGE_TOOL: dict[str, Any] = {
    "type": "function",
    "name": "search_knowledge",
    "description": "Find a known fix for an IT symptom. Call this before suggesting steps.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Short symptom, such as 'vpn will not connect'.",
            }
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


def live_url() -> str:
    return os.environ.get("OPENAI_LIVE_URL", LIVE_URL)


def live_model() -> str:
    return os.environ.get("OPENAI_LIVE_MODEL", LIVE_MODEL)


def live_voice() -> str:
    return os.environ.get("OPENAI_LIVE_VOICE", LIVE_VOICE)


def text_message(role: str, text: str) -> dict[str, Any]:
    """One history message. Developer and user use input_text. Assistant uses output_text."""
    content_type = "output_text" if role == "assistant" else "input_text"
    return {
        "type": "message",
        "role": role,
        "content": [{"type": content_type, "text": text}],
    }


def session_start(mode: str, caller_line: str | None = None, model: str | None = None, voice: str | None = None) -> dict[str, Any]:
    """The first client event. Nothing else is legal until `session.started`.

    A caller line is text history. A microphone session leaves it empty and
    sends speech later with `session.input_audio.append`.
    """
    if mode not in {"client", "responses"}:
        raise ValueError(f"unknown delegation mode: {mode}")
    if mode == "client":
        delegation: dict[str, Any] = {"type": "client"}
    else:
        delegation = {
            "type": "responses",
            "responses": {
                "model": os.environ.get("OPENAI_LIVE_BACKEND_MODEL", BACKEND_MODEL),
                "tools": [SEARCH_KNOWLEDGE_TOOL],
                "tool_choice": "auto",
            },
        }
    if caller_line:
        history = [
            text_message(
                "developer",
                "Speak first. The caller has already stated the problem in the user message. "
                "Acknowledge it in one sentence, then delegate before you suggest a fix.",
            ),
            text_message("user", caller_line),
        ]
    else:
        history = [
            text_message("developer", "Greet the caller in one short sentence, then listen."),
        ]
    return {
        "type": "session.start",
        "event_id": "event_start",
        "session": {
            "model": model or live_model(),
            "instructions": INSTRUCTIONS,
            "audio": {
                "format": {"type": "audio/pcm", "rate": SAMPLE_RATE},
                "output": {"voice": voice or live_voice()},
            },
            "delegation": delegation,
            "input": history,
        },
    }


def input_audio_append(pcm: bytes) -> dict[str, Any]:
    """One microphone chunk. Raw mono PCM16, even length, no WAV header."""
    if not pcm:
        raise ValueError("audio append must be non-empty")
    if len(pcm) % 2:
        raise ValueError("PCM16 needs an even number of bytes")
    return {
        "type": "session.input_audio.append",
        "audio": base64.b64encode(pcm).decode("ascii"),
    }
