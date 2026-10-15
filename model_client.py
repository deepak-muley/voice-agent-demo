"""OpenAI-compatible chat completions client.

`complete(messages, tools)` returns a `ModelTurn`. Point `base_url` at OpenAI,
Azure, vLLM, or Ollama. This module does not train a model.
"""

from __future__ import annotations

import json
import os
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


def _ssl_context() -> ssl.SSLContext:
    """CA bundle for HTTPS. A python.org macOS build ships with an empty store."""
    try:
        import certifi
    except ImportError:
        certifi = None
    if certifi is not None:
        return ssl.create_default_context(cafile=certifi.where())
    context = ssl.create_default_context()
    if context.cert_store_stats().get("x509_ca", 0) == 0:
        bundle = os.environ.get("SSL_CERT_FILE") or "/etc/ssl/cert.pem"
        if os.path.isfile(bundle):
            context.load_verify_locations(bundle)
    return context


_SSL_CONTEXT = _ssl_context()


def trace_json(title: str, payload: Any) -> None:
    """Print one exact JSON payload. The Authorization header is never included."""
    print("\n----- " + title + " -----")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def message_from_turn(turn: ModelTurn) -> dict[str, Any]:
    """Assistant message stored after a model reply. Used by the verbose trace."""
    message: dict[str, Any] = {"role": "assistant", "content": turn.content}
    if turn.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in turn.tool_calls
        ]
    return message


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class ModelTurn:
    content: str | None
    tool_calls: list[ToolCall]


class OpenAICompatibleClient:
    """POST /chat/completions. Point base_url at OpenAI or a self-hosted server."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0, verbose: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.verbose = verbose

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": "auto",
            "temperature": 0.2,
        }
        if self.verbose:
            trace_json("sent to model", body)
        payload = json.dumps(body).encode()
        url = f"{self.base_url}/chat/completions"
        last_error: Exception | None = None
        for attempt in range(3):
            request = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=self.timeout, context=_SSL_CONTEXT) as response:
                    raw = response.read().decode()
                data = json.loads(raw)
                if self.verbose:
                    trace_json("received from model", data)
                return _parse_completion(data)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                last_error = RuntimeError(f"model HTTP {exc.code}: {detail}")
                if exc.code not in {429, 500, 502, 503}:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = RuntimeError(f"model unreachable: {exc}")
            time.sleep(0.4 * (2**attempt))
        raise last_error or RuntimeError("model request failed")


def _parse_completion(data: dict[str, Any]) -> ModelTurn:
    choice = data["choices"][0]["message"]
    calls: list[ToolCall] = []
    for raw in choice.get("tool_calls") or []:
        fn = raw.get("function") or {}
        try:
            arguments = json.loads(fn.get("arguments") or "{}")
        except json.JSONDecodeError:
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        calls.append(ToolCall(id=raw.get("id") or f"call_{len(calls)}", name=fn.get("name") or "", arguments=arguments))
    content = choice.get("content")
    return ModelTurn(content=content if isinstance(content, str) else None, tool_calls=calls)
