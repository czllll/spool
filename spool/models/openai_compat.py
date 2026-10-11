"""Adapter for OpenAI-compatible chat completion endpoints.

One client covers OpenAI, DashScope (Qwen), DeepSeek, Moonshot, OpenRouter,
vLLM and llama.cpp's llama-server. Streaming is parsed by hand: content
deltas, reasoning deltas, and tool-call deltas whose JSON arguments arrive in
fragments keyed by index.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from typing import Any, Mapping

import httpx

from ..util import canonical_json
from .base import ModelError, ModelRequest, ModelResponse, parse_arguments
from .pricing import Price, cost_usd
from .retry import RetryPolicy, TokenBucket, with_retries
from .sse import SSEParser

RETRIABLE_STATUS = {408, 409, 429, 500, 502, 503, 504, 529}
_STOP_REASONS = {"stop": "end_turn", "length": "max_tokens", "tool_calls": "tool_use", "function_call": "tool_use"}
INLINE_TOOL_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.DOTALL)


class OpenAICompatClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key: str | None = None,
        name: str | None = None,
        extra_body: Mapping[str, Any] | None = None,
        stream: bool = True,
        timeout: float = 600.0,
        retry: RetryPolicy | None = None,
        rate_limiter: TokenBucket | None = None,
        prices: Mapping[str, Price] | None = None,
        price_key: str | None = None,
        max_tokens_field: str = "max_tokens",
        slot_param: str | None = None,
        default_slot: int | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        headers = {"content-type": "application/json"}
        if api_key:
            headers["authorization"] = f"Bearer {api_key}"
        self.name = name or model
        self.model = model
        self.extra_body = dict(extra_body or {})
        self.stream = stream
        self.retry = retry or RetryPolicy()
        self.rate_limiter = rate_limiter
        self.prices = prices
        self.price_key = price_key or model
        self.max_tokens_field = max_tokens_field
        # Session affinity: pin a session to one server slot so its KV cache is not
        # evicted by other sessions' requests (llama.cpp: `id_slot`).
        self.slot_param = slot_param
        self.default_slot = default_slot
        self._http = httpx.AsyncClient(
            base_url=base_url.rstrip("/"), headers=headers, timeout=httpx.Timeout(timeout, connect=15.0),
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ request body
    def build_body(self, request: ModelRequest) -> dict[str, Any]:
        messages: list[dict[str, Any]] = [{"role": "system", "content": request.system}]
        for msg in request.messages:
            messages.append(self._wire_message(msg))
        body: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": self.stream,
            self.max_tokens_field: request.max_tokens,
        }
        if request.tools:
            body["tools"] = [
                {"type": "function", "function": {"name": t["name"], "description": t["description"], "parameters": t["parameters"]}}
                for t in request.tools
            ]
        if request.temperature is not None:
            body["temperature"] = request.temperature
        if self.stream:
            body["stream_options"] = {"include_usage": True}
        slot = request.metadata.get("slot", self.default_slot)
        if self.slot_param and slot is not None:
            body[self.slot_param] = slot
        body.update(self.extra_body)
        return body

    @staticmethod
    def _wire_message(msg: Mapping[str, Any]) -> dict[str, Any]:
        role = msg["role"]
        if role == "assistant":
            out: dict[str, Any] = {"role": "assistant", "content": msg.get("content") or ""}
            calls = msg.get("tool_calls") or []
            if calls:
                out["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {
                            "name": c["name"],
                            # Send back the exact string the model produced: re-serializing
                            # would change bytes the server already holds in its KV cache.
                            "arguments": c.get("raw_arguments") if c.get("raw_arguments") is not None
                            else canonical_json(c.get("arguments") or {}),
                        },
                    }
                    for c in calls
                ]
            return out
        if role == "tool":
            return {"role": "tool", "tool_call_id": msg["tool_call_id"], "content": msg.get("content", "")}
        return {"role": role, "content": msg.get("content", "")}

    # ----------------------------------------------------------------- calling
    async def complete(self, request: ModelRequest) -> ModelResponse:
        return await with_retries(lambda: self._complete_once(request), self.retry)

    async def _complete_once(self, request: ModelRequest) -> ModelResponse:
        if self.rate_limiter is not None:
            await self.rate_limiter.acquire()
        body = self.build_body(request)
        started = time.perf_counter()
        try:
            if self.stream:
                acc = await self._post_stream(body)
            else:
                acc = await self._post_plain(body)
        except httpx.TimeoutException as exc:
            raise ModelError(f"request timed out: {exc}", retriable=True) from exc
        except httpx.TransportError as exc:
            raise ModelError(f"transport error: {exc}", retriable=True) from exc
        latency_ms = (time.perf_counter() - started) * 1000
        return self._finish(acc, latency_ms)

    async def _post_plain(self, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._http.post("/chat/completions", json=body)
        if resp.status_code != 200:
            raise _http_error(resp.status_code, resp.text, resp.headers)
        data = resp.json()
        if "error" in data:
            raise ModelError(f"provider error: {data['error']}", retriable=False)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        calls = []
        for i, tc in enumerate(message.get("tool_calls") or []):
            fn = tc.get("function") or {}
            calls.append({"index": i, "id": tc.get("id"), "name": fn.get("name", ""), "args": [fn.get("arguments") or ""]})
        return {
            "content": message.get("content") or "",
            "reasoning": message.get("reasoning_content") or message.get("reasoning") or "",
            "calls": calls,
            "finish": choice.get("finish_reason") or "stop",
            "usage": data.get("usage") or {},
            "timings": data.get("timings"),
            "model": data.get("model") or self.model,
        }

    async def _post_stream(self, body: dict[str, Any]) -> dict[str, Any]:
        acc: dict[str, Any] = {
            "content_parts": [], "reasoning_parts": [], "calls_by_index": {},
            "finish": None, "usage": {}, "timings": None, "model": self.model,
        }
        async with self._http.stream("POST", "/chat/completions", json=body) as resp:
            if resp.status_code != 200:
                text = (await resp.aread()).decode("utf-8", "replace")
                raise _http_error(resp.status_code, text, resp.headers)
            parser = SSEParser()
            async for line in resp.aiter_lines():
                event = parser.feed(line)
                if event is None:
                    continue
                if event.data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(event.data)
                except json.JSONDecodeError as exc:
                    raise ModelError(f"malformed stream chunk: {event.data[:200]!r}", retriable=True) from exc
                self._apply_chunk(acc, chunk)
        calls = [
            {"index": idx, **slot} for idx, slot in sorted(acc["calls_by_index"].items())
        ]
        return {
            "content": "".join(acc["content_parts"]),
            "reasoning": "".join(acc["reasoning_parts"]),
            "calls": calls,
            "finish": acc["finish"] or "stop",
            "usage": acc["usage"],
            "timings": acc["timings"],
            "model": acc["model"],
        }

    @staticmethod
    def _apply_chunk(acc: dict[str, Any], chunk: Mapping[str, Any]) -> None:
        if "error" in chunk:
            raise ModelError(f"provider error in stream: {chunk['error']}", retriable=False)
        if chunk.get("model"):
            acc["model"] = chunk["model"]
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                acc["content_parts"].append(delta["content"])
            reasoning = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning:
                acc["reasoning_parts"].append(reasoning)
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", len(acc["calls_by_index"]))
                slot = acc["calls_by_index"].setdefault(idx, {"id": None, "name": "", "args": []})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name"):
                    slot["name"] += fn["name"]
                if fn.get("arguments"):
                    slot["args"].append(fn["arguments"])
            if choice.get("finish_reason"):
                acc["finish"] = choice["finish_reason"]
        if chunk.get("usage"):
            acc["usage"] = chunk["usage"]
        if chunk.get("timings"):
            acc["timings"] = chunk["timings"]

    def _finish(self, acc: Mapping[str, Any], latency_ms: float) -> ModelResponse:
        tool_calls: list[dict[str, Any]] = []
        for slot in acc["calls"]:
            raw = "".join(slot["args"])
            args, error = parse_arguments(raw)
            tool_calls.append({
                "id": slot["id"] or f"call_{uuid.uuid4().hex[:12]}",
                "name": slot["name"],
                "arguments": args,
                "raw_arguments": raw,
                "parse_error": error,
            })
        content = acc["content"]
        inline = 0
        if not tool_calls and "<tool_call>" in content:
            # Some local chat templates leak tool calls into plain text when the
            # server-side parser misses them. Recover the well-formed ones.
            tool_calls, content, inline = _extract_inline_calls(content)
        usage = _normalize_usage(acc["usage"], acc.get("timings"))
        cost, known = cost_usd(self.price_key, usage, self.prices)
        usage["cost_usd"] = cost
        if not known:
            usage["cost_unknown"] = True
        if inline:
            usage["inline_tool_calls"] = inline
        stop = _STOP_REASONS.get(acc["finish"], acc["finish"])
        if tool_calls and stop == "end_turn":
            stop = "tool_use"
        return ModelResponse(
            content=content.strip(),
            tool_calls=tool_calls,
            stop_reason=stop,
            usage=usage,
            model=acc["model"],
            latency_ms=latency_ms,
            reasoning=acc["reasoning"] or None,
            timings=_compact_timings(acc.get("timings")),
        )


def _http_error(status: int, text: str, headers: Mapping[str, str]) -> ModelError:
    retry_after = None
    if headers.get("retry-after"):
        try:
            retry_after = float(headers["retry-after"])
        except ValueError:
            retry_after = None
    return ModelError(
        f"HTTP {status}: {text[:500]}", retriable=status in RETRIABLE_STATUS, status=status, retry_after=retry_after
    )


def _normalize_usage(usage: Mapping[str, Any], timings: Mapping[str, Any] | None) -> dict[str, Any]:
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cached = int(((usage.get("prompt_tokens_details") or {}).get("cached_tokens")) or 0)
    if timings and "cache_n" in timings:
        # llama.cpp reports reuse precisely: cache_n tokens reused, prompt_n recomputed.
        cached = int(timings.get("cache_n") or 0)
        prompt = max(prompt, cached + int(timings.get("prompt_n") or 0))
        completion = completion or int(timings.get("predicted_n") or 0)
    return {"input_tokens": prompt, "output_tokens": completion, "cached_tokens": cached, "cache_write_tokens": 0}


def _compact_timings(timings: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if not timings:
        return None
    keep = ("cache_n", "prompt_n", "prompt_ms", "predicted_n", "predicted_ms")
    return {k: round(timings[k], 1) if isinstance(timings[k], float) else timings[k] for k in keep if k in timings}


def _extract_inline_calls(content: str) -> tuple[list[dict[str, Any]], str, int]:
    calls: list[dict[str, Any]] = []
    for match in INLINE_TOOL_CALL.finditer(content):
        try:
            obj = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or "name" not in obj:
            continue
        args = obj.get("arguments") or {}
        if isinstance(args, str):
            args, error = parse_arguments(args)
        else:
            error = None if isinstance(args, dict) else "arguments must be an object"
        calls.append({
            "id": f"call_{uuid.uuid4().hex[:12]}",
            "name": str(obj["name"]),
            "arguments": args if isinstance(args, dict) else {},
            "raw_arguments": canonical_json(args) if isinstance(args, dict) else str(args),
            "parse_error": error,
        })
    remaining = INLINE_TOOL_CALL.sub("", content) if calls else content
    return calls, remaining, len(calls)
