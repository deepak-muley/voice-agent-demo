#!/usr/bin/env python3
"""GPT-Live lab.

A scripted trace shows the WebSocket events with no API key. `--live` opens
one real session, seeds a caller line as text, and saves the spoken audio.

Run from the repo root:
  python3 chatgpt_live/run.py                  # scripted client delegation
  python3 chatgpt_live/run.py --plugin         # scripted function tool
  python3 chatgpt_live/run.py --self-check
  python3 chatgpt_live/run.py --show-contract
  OPENAI_API_KEY=... python3 chatgpt_live/run.py --live   # microphone; Ctrl-C hangs up
  python3 chatgpt_live/run.py --live --line "Email will not sync"
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from contract import CALLER_LINE, input_audio_append, session_start
from lab import Lab, ScriptedServer, VPN_FIX, exchange


def print_event(side: str, event: dict[str, Any], verbose: bool) -> None:
    if verbose:
        print(f"\n----- {side} -----")
        print(json.dumps(event, indent=2, ensure_ascii=False))
        return
    kind = event.get("type")
    if kind == "session.start":
        session = event.get("session") or {}
        delegation = (session.get("delegation") or {}).get("type")
        voice = ((session.get("audio") or {}).get("output") or {}).get("voice")
        caller = ""
        for message in session.get("input") or []:
            if message.get("role") == "user":
                parts = message.get("content") or []
                caller = parts[0].get("text") if parts else ""
        print(f"client  session.start  model={session.get('model')} delegation={delegation} voice={voice}")
        print(f"        caller {caller!r}")
        return
    if kind == "session.commentary.append":
        print(f"client  session.commentary.append  delegation={event.get('delegation_id')}")
        print(f"        {event.get('content')}")
        return
    if kind == "response.item.create":
        item = event.get("item") or {}
        print(f"client  response.item.create  call_id={item.get('call_id')}")
        print(f"        {item.get('output')}")
        return
    if kind == "response.create":
        print("client  response.create")
        return
    if kind == "session.close":
        print("client  session.close")
        return
    if kind == "session.started":
        session = event.get("session") or {}
        print(f"server  session.started  id={session.get('id')}")
        return
    if kind == "session.input_transcript.delta":
        print(f"server  caller  +{event.get('delta')!r}")
        return
    if kind == "session.output_transcript.delta":
        print(f"server  voice   +{event.get('delta')!r}")
        return
    if kind == "session.delegation.created":
        delegation = event.get("delegation") or {}
        print(f"server  session.delegation.created  id={delegation.get('id')} target={delegation.get('target')}")
        if delegation.get("target") == "client":
            print("        caller text is the transcript above")
        else:
            print("        caller text is the transcript above; the function call carries the arguments")
        return
    if kind == "session.commentary.appended":
        print(f"server  session.commentary.appended  client_event_id={event.get('client_event_id')}")
        return
    if kind == "session.closed":
        usage = event.get("usage") or {}
        print(f"server  session.closed  reason={event.get('reason')} audio_seconds={usage.get('seconds')}")
        return
    if kind == "response.event":
        inner = event.get("event") or {}
        inner_type = inner.get("type")
        if inner_type == "response.output_item.done":
            item = inner.get("item") or {}
            print(f"server  function_call  name={item.get('name')} call_id={item.get('call_id')}")
            print(f"        arguments {item.get('arguments')}")
            return
        if inner_type == "response.output_text.delta":
            print(f"server  backend +{inner.get('delta')!r}")
            return
        print(f"server  response.event  {inner_type}")
        return
    print(f"{side}  {kind}")


def run_scripted(mode: str, verbose: bool) -> tuple[Lab, list[tuple[str, dict[str, Any]]]]:
    title = "client delegation" if mode == "client" else "responses delegation (plugin)"
    print(f"----- {title}, scripted -----")
    lab = Lab(mode)
    log = exchange(lab, ScriptedServer(mode))
    for side, event in log:
        print_event(side, event, verbose)
    print()
    print(f"spoken: {lab.output_transcript}")
    return lab, log


def show_contract() -> None:
    print("# client delegation, where the desk agent will attach\n")
    print(json.dumps(session_start("client", CALLER_LINE), indent=2))
    print("\n# responses delegation, where a plugin function will attach\n")
    print(json.dumps(session_start("responses", CALLER_LINE), indent=2))
    print("\n# one microphone chunk, once a live call streams audio\n")
    print(json.dumps(input_audio_append(b"\x00\x00" * 4), indent=2))


def _quiet(mode: str) -> tuple[Lab, list[tuple[str, dict[str, Any]]]]:
    lab = Lab(mode)
    return lab, exchange(lab, ScriptedServer(mode))


def self_check() -> None:
    odd = b"\x00"
    try:
        input_audio_append(odd)
    except ValueError:
        pass
    else:
        raise AssertionError("odd PCM length was accepted")
    chunk = input_audio_append(b"\x01\x00\x02\x00")
    assert chunk["type"] == "session.input_audio.append"
    assert chunk["audio"]
    mic_session = session_start("client", None)
    assert [message["role"] for message in mic_session["session"]["input"]] == ["developer"]

    lab, log = _quiet("client")
    client_events = [event for side, event in log if side == "client"]
    assert client_events[0]["type"] == "session.start"
    assert client_events[0]["session"]["delegation"] == {"type": "client"}
    assert client_events[0]["session"]["model"] == "gpt-live-1"
    commentary = next(event for event in client_events if event["type"] == "session.commentary.append")
    assert commentary["delegation_id"] == "item_lab_1"
    assert commentary["content"] == VPN_FIX
    assert "INC" not in commentary["content"]
    assert lab.output_transcript == VPN_FIX
    assert log[-1][1]["type"] == "session.closed"
    # The delegation event is metadata. The fix was chosen from the transcript.
    delegation = next(event for side, event in log if event.get("type") == "session.delegation.created")
    assert "VPN" not in json.dumps(delegation["delegation"])

    plugin, plugin_log = _quiet("responses")
    plugin_client = [event for side, event in plugin_log if side == "client"]
    assert plugin_client[0]["session"]["delegation"]["type"] == "responses"
    tool = plugin_client[0]["session"]["delegation"]["responses"]["tools"][0]
    assert tool["name"] == "search_knowledge"
    assert "function" not in tool
    result = next(event for event in plugin_client if event["type"] == "response.item.create")
    assert result["item"]["call_id"] == "call_kb"
    assert "KB001" in result["item"]["output"]
    create_at = next(i for i, event in enumerate(plugin_client) if event["type"] == "response.create")
    result_at = next(i for i, event in enumerate(plugin_client) if event["type"] == "response.item.create")
    assert result_at < create_at
    assert "full-tunnel" in plugin.output_transcript

    unknown = Lab("responses")
    replies = unknown.handle(
        {
            "type": "response.event",
            "delegation_id": "item_x",
            "event": {
                "type": "response.output_item.done",
                "item": {
                    "type": "function_call",
                    "call_id": "call_nope",
                    "name": "list_all_users",
                    "arguments": "{}",
                },
            },
        }
    )
    assert replies[0]["item"]["call_id"] == "call_nope"
    assert json.loads(replies[0]["item"]["output"])["error"] == "unknown_tool"
    assert replies[1]["type"] == "response.create"
    print("self-check ok")
    print(f"  client delegation id: {commentary['delegation_id']}")
    print(f"  commentary: {commentary['content']}")
    print(f"  plugin call: {result['item']['call_id']}")
    print(f"  spoken after plugin: {plugin.output_transcript}")


def main() -> None:
    parser = argparse.ArgumentParser(description="GPT-Live lab")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--show-contract", action="store_true")
    parser.add_argument("--plugin", action="store_true", help="scripted responses delegation with one function tool")
    parser.add_argument("--live", action="store_true", help="open the microphone and talk; Ctrl-C hangs up")
    parser.add_argument("--line", default=None, help="seed this text and leave the microphone closed")
    parser.add_argument("--verbose", action="store_true", help="print each event as JSON")
    parser.add_argument("--no-play", action="store_true", help="skip afplay of the saved wav")
    args = parser.parse_args()
    if args.show_contract:
        show_contract()
    elif args.live:
        from live import run_live

        run_live(args.line, verbose=args.verbose, play=not args.no_play, mic=args.line is None)
    elif args.self_check:
        self_check()
    elif args.plugin:
        run_scripted("responses", verbose=args.verbose)
    else:
        run_scripted("client", verbose=args.verbose)


if __name__ == "__main__":
    main()
