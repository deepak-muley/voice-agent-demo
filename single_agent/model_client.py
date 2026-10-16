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


@dataclass
class ModelRequestStats:
    """One chat completion, successful or not."""

    index: int
    model: str
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cached_tokens: int | None
    reasoning_tokens: int | None
    latency_ms: float
    finish_reason: str
    ok: bool
    retries: int


class UsageLedger:
    """Token and request counters for one live session.

    OpenAI reports usage on each completion. This ledger keeps every request
    and the session totals. ``--usage`` prints ``format()`` when the session ends.
    """

    def __init__(self) -> None:
        self.requests: list[ModelRequestStats] = []
        self.failures: int = 0
        self.retries: int = 0

    def record_openai(self, data: dict[str, Any], latency_ms: float, retries: int = 0) -> None:
        """Record one HTTP completion. ``data`` is the chat.completion body."""
        self.retries += retries
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else None
        choice = (data.get("choices") or [{}])[0]
        finish = ""
        if isinstance(choice, dict):
            finish = str(choice.get("finish_reason") or "")
        prompt, completion, total, cached, reasoning = _tokens_from_openai_usage(usage)
        self.requests.append(
            ModelRequestStats(
                index=len(self.requests) + 1,
                model=str(data.get("model") or ""),
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                cached_tokens=cached,
                reasoning_tokens=reasoning,
                latency_ms=latency_ms,
                finish_reason=finish,
                ok=True,
                retries=retries,
            )
        )

    def record_langchain(self, message: Any, latency_ms: float) -> None:
        """Record one LangChain ``AIMessage``. Usage may be missing on a local model."""
        meta = _mapping(getattr(message, "response_metadata", None))
        usage_meta = getattr(message, "usage_metadata", None)
        if usage_meta:
            prompt = _as_int(_get(usage_meta, "input_tokens"))
            completion = _as_int(_get(usage_meta, "output_tokens"))
            total = _as_int(_get(usage_meta, "total_tokens"))
            cached = _detail_int(_get(usage_meta, "input_token_details"), "cache_read", "cached_tokens")
            reasoning = _detail_int(_get(usage_meta, "output_token_details"), "reasoning", "reasoning_tokens")
            if prompt is None and completion is None and total is None:
                prompt, completion, total, cached, reasoning = _tokens_from_openai_usage(
                    _mapping(meta.get("token_usage") or meta.get("usage"))
                )
            elif total is None and prompt is not None and completion is not None:
                total = prompt + completion
            if cached is None:
                cached = 0
            if reasoning is None:
                reasoning = 0
        else:
            prompt, completion, total, cached, reasoning = _tokens_from_openai_usage(
                _mapping(meta.get("token_usage") or meta.get("usage")) or None
            )
        model = str(meta.get("model_name") or meta.get("model") or "")
        finish = str(meta.get("finish_reason") or "")
        self.requests.append(
            ModelRequestStats(
                index=len(self.requests) + 1,
                model=model,
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                cached_tokens=cached,
                reasoning_tokens=reasoning,
                latency_ms=latency_ms,
                finish_reason=finish,
                ok=True,
                retries=0,
            )
        )

    def record_failure(self, latency_ms: float, retries: int = 0) -> None:
        self.retries += retries
        self.failures += 1
        self.requests.append(
            ModelRequestStats(
                index=len(self.requests) + 1,
                model="",
                prompt_tokens=None,
                completion_tokens=None,
                total_tokens=None,
                cached_tokens=None,
                reasoning_tokens=None,
                latency_ms=latency_ms,
                finish_reason="error",
                ok=False,
                retries=retries,
            )
        )

    def format(self) -> str:
        """Per-request tokens, then session totals."""
        ok = [row for row in self.requests if row.ok]
        lines = [
            "----- usage report -----",
            f"model requests: {len(ok)}    failed: {self.failures}    http retries: {self.retries}",
        ]
        models = sorted({row.model for row in self.requests if row.model})
        if models:
            lines.append("models: " + ", ".join(models))
        if not self.requests:
            lines.append("(no model requests)")
            return "\n".join(lines)

        lines.append(
            f"{'#':>3}  {'prompt':>10}  {'completion':>10}  {'total':>10}  {'cached':>10}  {'reasoning':>9}  {'ms':>7}  finish"
        )
        for row in self.requests:
            lines.append(
                f"{row.index:>3}  {_num(row.prompt_tokens):>10}  {_num(row.completion_tokens):>10}  "
                f"{_num(row.total_tokens):>10}  {_num(row.cached_tokens):>10}  {_num(row.reasoning_tokens):>9}  "
                f"{row.latency_ms:>7.0f}  {row.finish_reason or '-'}"
            )

        prompt = _sum_field(ok, "prompt_tokens")
        completion = _sum_field(ok, "completion_tokens")
        total = _sum_field(ok, "total_tokens")
        cached = _sum_field(ok, "cached_tokens")
        reasoning = _sum_field(ok, "reasoning_tokens")
        latency_ms = sum(row.latency_ms for row in self.requests)
        reported = [row for row in ok if row.total_tokens is not None]
        missing = len(ok) - len(reported)
        lines.append("")
        lines.append(f"prompt tokens:           {_num(prompt)}")
        lines.append(f"completion tokens:       {_num(completion)}")
        lines.append(f"total tokens:            {_num(total)}")
        lines.append(f"cached prompt tokens:    {_num(cached)}")
        if prompt is not None and cached is not None:
            lines.append(f"uncached prompt tokens:  {_num(prompt - cached)}")
            if prompt:
                lines.append(f"cache hit:               {100 * cached / prompt:.1f}%")
        lines.append(f"reasoning tokens:        {_num(reasoning)}")
        if reported:
            average = sum(row.total_tokens or 0 for row in reported) / len(reported)
            lines.append(f"tokens per request:      {average:,.1f}")
            if len(reported) > 1:
                totals = [row.total_tokens or 0 for row in reported]
                lines.append(f"min / max tokens:        {min(totals):,} / {max(totals):,}")
        lines.append(f"latency:                 {latency_ms / 1000:.2f} s")
        if missing:
            lines.append(f"requests without usage:  {missing}")
        return "\n".join(lines)


def _mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if value is None:
        return {}
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    raw = getattr(value, "__dict__", None)
    if isinstance(raw, dict):
        return {key: item for key, item in raw.items() if not key.startswith("_")}
    return {}


def _get(value: Any, key: str, default: Any = None) -> Any:
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _detail_int(details: Any, *keys: str) -> int | None:
    if not details:
        return None
    for key in keys:
        found = _as_int(_get(details, key))
        if found is not None:
            return found
    return 0


def _tokens_from_openai_usage(
    usage: dict[str, Any] | None,
) -> tuple[int | None, int | None, int | None, int | None, int | None]:
    """prompt, completion, total, cached, reasoning. None when the body omitted usage."""
    if not usage:
        return None, None, None, None, None
    prompt = _as_int(usage.get("prompt_tokens")) or 0
    completion = _as_int(usage.get("completion_tokens")) or 0
    total = _as_int(usage.get("total_tokens"))
    if total is None:
        total = prompt + completion
    prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    completion_details = (
        usage.get("completion_tokens_details") if isinstance(usage.get("completion_tokens_details"), dict) else {}
    )
    cached = _as_int(prompt_details.get("cached_tokens")) or 0
    reasoning = _as_int(completion_details.get("reasoning_tokens")) or 0
    return prompt, completion, total, cached, reasoning


def _num(value: int | None) -> str:
    if value is None:
        return "-"
    return f"{value:,}"


def _sum_field(rows: list[ModelRequestStats], name: str) -> int | None:
    values = [getattr(row, name) for row in rows if getattr(row, name) is not None]
    if not values:
        return None
    return sum(values)


class OpenAICompatibleClient:
    """POST /chat/completions. Point base_url at OpenAI or a self-hosted server."""

    def __init__(self, base_url: str, api_key: str, model: str, timeout: float = 30.0, verbose: bool = False) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout
        self.verbose = verbose
        self.usage = UsageLedger()

    def complete(self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> ModelTurn:
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": 0.2,
        }
        # The router and the voice agent call complete() with no tools.
        # An empty tools array is rejected by some compatible servers.
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        if self.verbose:
            trace_json("sent to model", body)
        payload = json.dumps(body).encode()
        url = f"{self.base_url}/chat/completions"
        last_error: Exception | None = None
        started = time.perf_counter()
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
                self.usage.record_openai(data, (time.perf_counter() - started) * 1000, retries=attempt)
                if self.verbose:
                    trace_json("received from model", data)
                return _parse_completion(data)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode(errors="replace")[:500]
                last_error = RuntimeError(f"model HTTP {exc.code}: {detail}")
                if exc.code not in {429, 500, 502, 503}:
                    self.usage.record_failure((time.perf_counter() - started) * 1000, retries=attempt)
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = RuntimeError(f"model unreachable: {exc}")
            time.sleep(0.4 * (2**attempt))
        self.usage.record_failure((time.perf_counter() - started) * 1000, retries=2)
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
