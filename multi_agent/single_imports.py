"""Load the single-agent modules so this folder does not copy them.

The sandbox, the speech guards, the tool schemas, and the HTTP client stay in
``single_agent/``. Multi-agent code only adds roles and the handoff between them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SINGLE = Path(__file__).resolve().parents[1] / "single_agent"
if str(_SINGLE) not in sys.path:
    sys.path.append(str(_SINGLE))

from agent import MAX_STEPS, AuditEvent, SpeechReply, normalize_speech, redact
from model_client import ModelTurn, OpenAICompatibleClient, ToolCall, message_from_turn, trace_json
from voice_agent import DEMO_TURNS, TOOLS, ScriptedModel, ServiceNowSandbox, build_agent, run_turns

__all__ = [
    "DEMO_TURNS",
    "MAX_STEPS",
    "TOOLS",
    "AuditEvent",
    "ModelTurn",
    "OpenAICompatibleClient",
    "ScriptedModel",
    "ServiceNowSandbox",
    "SpeechReply",
    "ToolCall",
    "build_agent",
    "message_from_turn",
    "normalize_speech",
    "redact",
    "run_turns",
    "trace_json",
]
