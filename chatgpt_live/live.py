"""WebSocket client for one live GPT-Live session.

`--self-check` and the scripted trace never import this module. `--live`
does. The `websockets` package is already pinned in requirements.txt.
"""

from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import wave
from typing import Any

from contract import SAMPLE_RATE, input_audio_append, live_url, session_start
from lab import Lab

# 100 ms of mono PCM16 at 24 kHz. ffmpeg delivers this at capture speed.
MIC_CHUNK = SAMPLE_RATE * 2 // 10
_PLAYER_ERRORS: list[str] = []


def _ssl_context() -> ssl.SSLContext:
    """CA bundle for WSS. A python.org macOS build ships with an empty store."""
    try:
        import certifi
    except ImportError:
        certifi = None  # type: ignore[assignment]
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) == 0:
        bundle = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/cert.pem"
        if os.path.isfile(bundle):
            context.load_verify_locations(bundle)
    return context


def _public_event(event: dict[str, Any]) -> dict[str, Any]:
    """Copy an event with audio bytes replaced by a length, for the verbose log."""
    if event.get("type") == "session.output_audio.delta":
        raw = event.get("delta") or ""
        shown = dict(event)
        shown["delta"] = f"<{len(base64.b64decode(raw))} bytes>"
        return shown
    if event.get("type") == "session.input_audio.append":
        shown = dict(event)
        shown["audio"] = f"<{len(base64.b64decode(event.get('audio') or ''))} bytes>"
        return shown
    return event


def write_wav(pcm: bytes, path: str) -> None:
    with wave.open(path, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(pcm)


def run_live(
    caller_line: str | None,
    verbose: bool = False,
    play: bool = True,
    mic: bool = False,
    on_client: Any = None,
) -> Lab:
    """Open one client-delegation session.

    `mic` streams the microphone until Ctrl-C. A caller line seeds text history
    and leaves the microphone closed.
    """
    if mic:
        return _run_mic(verbose=verbose, play=play, on_client=on_client)
    if not caller_line:
        print("Pass --line to seed text, or omit it to open the microphone.", file=sys.stderr)
        sys.exit(1)
    return _run_text(caller_line, verbose=verbose, play=play)


def _run_text(caller_line: str, verbose: bool = False, play: bool = True) -> Lab:
    """Seed one caller line as text history and wait for a spoken reply."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        print("Set OPENAI_API_KEY.", file=sys.stderr)
        sys.exit(1)
    try:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect
    except ImportError:
        print(
            "--live needs the websockets package pinned in requirements.txt.\n"
            "  uv venv --python 3.12 .venv && uv pip install -r requirements.txt\n"
            "  .venv/bin/python chatgpt_live/run.py --live",
            file=sys.stderr,
        )
        sys.exit(1)

    lab = Lab("client", caller_line=caller_line)
    audio = bytearray()
    url = live_url()
    print(f"connecting {url}")
    with connect(
        url,
        additional_headers={"Authorization": f"Bearer {api_key}"},
        ssl=_ssl_context(),
        max_size=8 * 1024 * 1024,
        open_timeout=20,
    ) as ws:
        _send(ws, lab.session_start(), verbose)
        deadline = time.monotonic() + 45
        result_at: float | None = None
        remote_closed = False
        try:
            while time.monotonic() < deadline:
                if lab.spoke_after_result:
                    timeout = 2.0
                elif result_at is not None:
                    timeout = 12.0
                else:
                    timeout = 20.0
                timeout = min(timeout, max(0.1, deadline - time.monotonic()))
                try:
                    raw = ws.recv(timeout=timeout)
                except TimeoutError:
                    if lab.spoke_after_result or (result_at is not None and time.monotonic() - result_at >= 12):
                        break
                    continue
                except ConnectionClosed as exc:
                    remote_closed = True
                    print(f"connection closed: {exc}", file=sys.stderr)
                    break
                event = json.loads(raw)
                if event.get("type") == "session.output_audio.delta":
                    audio.extend(base64.b64decode(event.get("delta") or ""))
                    if verbose:
                        print(f"server  audio {len(audio)} bytes")
                    continue
                _print_server(event, verbose)
                if event.get("type") == "error":
                    err = event.get("error") or {}
                    print(f"error   {err.get('code')} {err.get('message')}", file=sys.stderr)
                for outgoing in lab.handle(event):
                    _send(ws, outgoing, verbose)
                    if outgoing.get("type") in {"session.commentary.append", "response.create"}:
                        result_at = time.monotonic()
                if event.get("type") == "session.closed":
                    return _finish(lab, audio, play)
        except KeyboardInterrupt:
            print()
        if remote_closed:
            return _finish(lab, audio, play)
        close = {"type": "session.close", "event_id": "event_close"}
        try:
            _send(ws, close, verbose)
            raw = ws.recv(timeout=15)
            closed = json.loads(raw)
            _print_server(closed, verbose)
            lab.handle(closed)
        except (TimeoutError, OSError) as exc:
            print(f"session.closed did not arrive: {exc}", file=sys.stderr)
    return _finish(lab, audio, play, already_played=False)


def _run_mic(verbose: bool = False, play: bool = True, on_client: Any = None) -> Lab:
    """Stream the microphone until Ctrl-C, and play speech as it arrives."""
    _require_ffmpeg()
    ws_mod = _websockets()
    api_key = os.environ["OPENAI_API_KEY"] if os.environ.get("OPENAI_API_KEY") else ""
    if not api_key:
        print("Set OPENAI_API_KEY.", file=sys.stderr)
        sys.exit(1)
    connect = ws_mod["connect"]
    ConnectionClosed = ws_mod["ConnectionClosed"]

    lab = Lab("client", caller_line="", on_client=on_client)
    audio = bytearray()
    mic_audio: queue.Queue[bytes | None] = queue.Queue()
    play_audio: queue.Queue[bytes | None] = queue.Queue()
    stop = threading.Event()
    mic_errors: list[str] = []
    mic_proc: subprocess.Popen[bytes] | None = None
    url = live_url()
    print(f"connecting {url}")
    print("The reply plays through the speakers. Lines below are the transcript.")
    print("Headphones keep that reply out of the microphone.")
    player_holder: list[subprocess.Popen[bytes] | None] = [None]
    player_thread: threading.Thread | None = None
    if play:
        player_holder, player_thread = _start_player(play_audio, stop)
    try:
        with connect(
            url,
            additional_headers={"Authorization": f"Bearer {api_key}"},
            ssl=_ssl_context(),
            max_size=8 * 1024 * 1024,
            open_timeout=20,
        ) as ws:
            _send(ws, session_start("client", None), verbose)
            started = False
            remote_closed = False
            start_deadline = time.monotonic() + 30
            try:
                while True:
                    if not started and time.monotonic() > start_deadline:
                        print("session.started did not arrive.", file=sys.stderr)
                        break
                    try:
                        raw = ws.recv(timeout=0.05)
                    except TimeoutError:
                        raw = None
                    except ConnectionClosed as exc:
                        remote_closed = True
                        print(f"connection closed: {exc}", file=sys.stderr)
                        break
                    if raw:
                        event = json.loads(raw)
                        if event.get("type") == "session.output_audio.delta":
                            pcm = base64.b64decode(event.get("delta") or "")
                            audio.extend(pcm)
                            if play and pcm:
                                play_audio.put(pcm)
                            if verbose:
                                print(f"server  audio {len(audio)} bytes")
                            if not _send_mic(ws, mic_audio, verbose):
                                _report_mic_failure(mic_proc, mic_errors)
                                break
                            continue
                        _print_server(event, verbose)
                        if event.get("type") == "error":
                            err = event.get("error") or {}
                            print(f"error   {err.get('code')} {err.get('message')}", file=sys.stderr)
                        if event.get("type") == "session.started" and not started:
                            started = True
                            print("Speak now. Ctrl-C hangs up.")
                            mic_proc = _start_mic(mic_audio, stop, mic_errors)
                        for outgoing in lab.handle(event):
                            _send(ws, outgoing, verbose)
                        if event.get("type") == "session.closed":
                            remote_closed = True
                            break
                    if not _send_mic(ws, mic_audio, verbose):
                        _report_mic_failure(mic_proc, mic_errors)
                        break
                    if mic_proc is not None and mic_proc.poll() is not None and mic_audio.empty():
                        _report_mic_failure(mic_proc, mic_errors)
                        break
            except KeyboardInterrupt:
                print()
            if not remote_closed:
                close = {"type": "session.close", "event_id": "event_close"}
                try:
                    _send(ws, close, verbose)
                    _drain_until_closed(ws, lab, play_audio if play else None, verbose)
                except (TimeoutError, OSError, ConnectionClosed) as exc:
                    print(f"session.closed did not arrive: {exc}", file=sys.stderr)
    finally:
        stop.set()
        _stop_process(mic_proc)
        play_audio.put(None)
        if player_thread is not None:
            player_thread.join(timeout=2)
        _stop_process(player_holder[0])
    return _finish(lab, audio, play=play, already_played=play)


def _require_ffmpeg() -> None:
    if shutil.which("ffmpeg"):
        return
    print(
        "ffmpeg opens the microphone. Install it, or seed text with --line.",
        file=sys.stderr,
    )
    sys.exit(1)


def _websockets() -> dict[str, Any]:
    try:
        from websockets.exceptions import ConnectionClosed
        from websockets.sync.client import connect
    except ImportError:
        print(
            "--live needs the websockets package pinned in requirements.txt.\n"
            "  uv venv --python 3.12 .venv && uv pip install -r requirements.txt\n"
            "  .venv/bin/python chatgpt_live/run.py --live",
            file=sys.stderr,
        )
        sys.exit(1)
    return {"connect": connect, "ConnectionClosed": ConnectionClosed}


def _mic_command() -> list[str]:
    device = os.environ.get("OPENAI_LIVE_AUDIO_DEVICE", "0" if sys.platform == "darwin" else "default")
    if sys.platform == "darwin":
        source = ["-f", "avfoundation", "-i", f":{device}"]
    else:
        source = ["-f", "pulse", "-i", device]
    return [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        *source,
        "-ac",
        "1",
        "-ar",
        str(SAMPLE_RATE),
        "-f",
        "s16le",
        "-acodec",
        "pcm_s16le",
        "pipe:1",
    ]


def _start_mic(chunks: queue.Queue[bytes | None], stop: threading.Event, errors: list[str]) -> subprocess.Popen[bytes]:
    proc = subprocess.Popen(
        _mic_command(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    threading.Thread(target=_read_mic, args=(proc, chunks, stop), daemon=True).start()
    threading.Thread(target=_read_stderr, args=(proc, errors), daemon=True).start()
    return proc


def _read_mic(proc: subprocess.Popen[bytes], chunks: queue.Queue[bytes | None], stop: threading.Event) -> None:
    pending = b""
    stream = proc.stdout
    assert stream is not None
    while not stop.is_set():
        piece = stream.read(MIC_CHUNK)
        if not piece:
            break
        pending += piece
        even = len(pending) - (len(pending) % 2)
        if even:
            chunks.put(pending[:even])
            pending = pending[even:]
    chunks.put(None)


def _read_stderr(proc: subprocess.Popen[bytes], errors: list[str]) -> None:
    if proc.stderr is None:
        return
    text = proc.stderr.read().decode("utf-8", "replace").strip()
    if text:
        errors.append(text)


def _send_mic(ws: Any, chunks: queue.Queue[bytes | None], verbose: bool) -> bool:
    """Send queued microphone audio. False when the capture process ended."""
    sent = 0
    while True:
        try:
            pcm = chunks.get_nowait()
        except queue.Empty:
            break
        if pcm is None:
            return False
        ws.send(json.dumps(input_audio_append(pcm)))
        sent += len(pcm)
    if verbose and sent:
        print(f"client  mic {sent} bytes")
    return True


def _report_mic_failure(proc: subprocess.Popen[bytes] | None, errors: list[str]) -> None:
    time.sleep(0.3)
    print("The microphone closed.", file=sys.stderr)
    for line in errors:
        print(line, file=sys.stderr)
    if sys.platform == "darwin":
        print(
            "Allow this terminal under System Settings → Privacy & Security → Microphone, then run again.",
            file=sys.stderr,
        )
    if proc is not None and proc.returncode is None:
        proc.poll()


def _player_command() -> list[str]:
    """Play raw PCM as it arrives. ffplay rejects -ac and exits before any sample."""
    command = [
        "ffmpeg",
        "-nostdin",
        "-hide_banner",
        "-loglevel",
        "error",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "32",
        "-analyzeduration",
        "0",
        "-f",
        "s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        "1",
        "-i",
        "pipe:0",
    ]
    if sys.platform == "darwin":
        return [*command, "-f", "audiotoolbox", "-"]
    return [*command, "-f", "pulse", "gpt-live"]


# 20 ms at 24 kHz mono PCM16. Playback is paced to this so the speaker device
# is not flooded in a burst or starved in the quiet between turns.
PLAY_FRAME = SAMPLE_RATE * 2 // 50
PLAY_LIMIT = SAMPLE_RATE * 2 * 3


def _start_player(
    chunks: queue.Queue[bytes | None], stop: threading.Event
) -> tuple[list[subprocess.Popen[bytes] | None], threading.Thread]:
    holder: list[subprocess.Popen[bytes] | None] = [None]
    thread = threading.Thread(target=_write_player, args=(holder, chunks, stop), daemon=True)
    thread.start()
    return holder, thread


def _open_speaker(holder: list[subprocess.Popen[bytes] | None]) -> None:
    _stop_process(holder[0])
    proc = subprocess.Popen(
        _player_command(),
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
        bufsize=0,
        start_new_session=True,
    )
    holder[0] = proc
    threading.Thread(target=_read_stderr, args=(proc, _PLAYER_ERRORS), daemon=True).start()


def _take_audio(chunks: queue.Queue[bytes | None], pending: bytes, block: float) -> tuple[bytes, bool]:
    """Pull queued PCM. The bool is False when the caller asked the player to stop."""
    if block > 0 and len(pending) < PLAY_FRAME:
        try:
            incoming = chunks.get(timeout=block)
        except queue.Empty:
            incoming = b""
        batch = [incoming]
    else:
        batch = []
    while True:
        try:
            batch.append(chunks.get_nowait())
        except queue.Empty:
            break
    for incoming in batch:
        if incoming is None:
            return pending, False
        pending += incoming
    if len(pending) > PLAY_LIMIT:
        pending = pending[-PLAY_LIMIT:]
    return pending, True


def _write_player(
    holder: list[subprocess.Popen[bytes] | None],
    chunks: queue.Queue[bytes | None],
    stop: threading.Event,
) -> None:
    """Write one 20 ms frame every 20 ms, from the moment the speaker opens.

    Zeros before the first word, and zeros between turns, are the same bytes.
    Waiting for speech, or waiting extra inside the loop, starves AudioToolbox
    and the process exits. A burst is held in `pending` and released on the clock.
    """
    _open_speaker(holder)
    pending = b""
    restarts = 0
    next_frame = time.monotonic()
    while not stop.is_set():
        pending, alive = _take_audio(chunks, pending, 0)
        if not alive or stop.is_set():
            break
        proc = holder[0]
        if proc is None or proc.poll() is not None:
            if restarts >= 3:
                _show_player_errors()
                break
            restarts += 1
            print("Speaker stopped. Starting it again.", file=sys.stderr)
            _show_player_errors()
            _PLAYER_ERRORS.clear()
            _open_speaker(holder)
            next_frame = time.monotonic()
            continue
        if len(pending) >= PLAY_FRAME:
            frame = pending[:PLAY_FRAME]
            pending = pending[PLAY_FRAME:]
        else:
            frame = pending + (b"\x00" * (PLAY_FRAME - len(pending)))
            pending = b""
        stream = proc.stdin
        if stream is None:
            continue
        try:
            stream.write(frame)
            stream.flush()
        except (BrokenPipeError, OSError):
            continue
        next_frame += PLAY_FRAME / (SAMPLE_RATE * 2)
        delay = next_frame - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        else:
            next_frame = time.monotonic()
    proc = holder[0]
    if proc is not None and proc.stdin is not None:
        try:
            proc.stdin.close()
        except OSError:
            pass


def _show_player_errors() -> None:
    time.sleep(0.05)
    if not _PLAYER_ERRORS:
        return
    print("The speaker closed.", file=sys.stderr)
    for line in _PLAYER_ERRORS:
        print(line, file=sys.stderr)


def _drain_until_closed(ws: Any, lab: Lab, play_audio: queue.Queue[bytes | None] | None, verbose: bool) -> None:
    """Read until session.closed. Audio can still arrive after session.close."""
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        timeout = max(0.1, deadline - time.monotonic())
        raw = ws.recv(timeout=timeout)
        event = json.loads(raw)
        if event.get("type") == "session.output_audio.delta":
            pcm = base64.b64decode(event.get("delta") or "")
            if play_audio is not None and pcm:
                play_audio.put(pcm)
            continue
        _print_server(event, verbose)
        lab.handle(event)
        if event.get("type") == "session.closed":
            return
    print("session.closed did not arrive.", file=sys.stderr)


def _stop_process(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def _send(ws: Any, event: dict[str, Any], verbose: bool) -> None:
    if verbose:
        print("\n----- client -----")
        print(json.dumps(_public_event(event), indent=2, ensure_ascii=False))
    else:
        _print_client(event)
    ws.send(json.dumps(event))


def _print_client(event: dict[str, Any]) -> None:
    kind = event.get("type")
    if kind == "session.start":
        session = event.get("session") or {}
        delegation = (session.get("delegation") or {}).get("type")
        audio = session.get("audio") or {}
        voice = ((audio.get("output") or {}).get("voice"))
        caller = ""
        for message in session.get("input") or []:
            if message.get("role") == "user":
                parts = message.get("content") or []
                caller = parts[0].get("text") if parts else ""
        print(f"client  session.start  model={session.get('model')} delegation={delegation} voice={voice}")
        if caller:
            print(f"        caller {caller!r}")
        else:
            print("        microphone")
        return
    if kind == "session.commentary.append":
        print(f"client  session.commentary.append  delegation={event.get('delegation_id')}")
        print(f"        {event.get('content')}")
        return
    if kind == "session.close":
        print("client  session.close")
        return
    print(f"client  {kind}")


def _print_server(event: dict[str, Any], verbose: bool) -> None:
    if verbose:
        print("\n----- server -----")
        print(json.dumps(_public_event(event), indent=2, ensure_ascii=False))
        return
    kind = event.get("type")
    if kind == "session.started":
        session = event.get("session") or {}
        print(f"server  session.started  id={session.get('id')} model={session.get('model')}")
    elif kind == "session.input_transcript.delta":
        print(f"server  caller  +{event.get('delta')!r}")
    elif kind == "session.output_transcript.delta":
        print(f"server  voice   +{event.get('delta')!r}")
    elif kind == "session.delegation.created":
        delegation = event.get("delegation") or {}
        print(f"server  session.delegation.created  id={delegation.get('id')} target={delegation.get('target')}")
        print("        caller text is the transcript above")
    elif kind == "session.commentary.appended":
        print(f"server  session.commentary.appended  client_event_id={event.get('client_event_id')}")
    elif kind == "session.usage.updated":
        usage = event.get("usage") or {}
        print(f"server  session.usage.updated  audio_seconds={usage.get('seconds')}")
    elif kind == "session.closed":
        usage = event.get("usage") or {}
        print(f"server  session.closed  reason={event.get('reason')} audio_seconds={usage.get('seconds')}")
    elif kind == "error":
        err = event.get("error") or {}
        print(f"server  error  {err.get('code')} {err.get('message')}")
    elif kind == "response.event":
        inner = event.get("event") or {}
        print(f"server  response.event  {inner.get('type')}  delegation={event.get('delegation_id')}")
    else:
        print(f"server  {kind}")


def _finish(lab: Lab, audio: bytearray, play: bool, already_played: bool = False) -> Lab:
    print()
    print(f"caller transcript: {lab.input_transcript!r}")
    print(f"spoken: {lab.output_transcript!r}")
    print(f"delegations: {', '.join(lab.delegation_ids) or '(none)'}")
    if not audio:
        print("audio: none")
        return lab
    path = os.path.join(tempfile.gettempdir(), "gpt-live-reply.wav")
    write_wav(bytes(audio), path)
    print(f"audio: {len(audio)} bytes  {path}")
    if play and not already_played and sys.platform == "darwin" and shutil.which("afplay"):
        subprocess.run(["afplay", path], check=False)
    return lab

