"""Adapter for the Anthropic Messages API, built on the official `anthropic` SDK.

Install with `pip install 'spool[anthropic]'`. Not exercised against the live
API in this repo's test suite (no key in CI); the conversion logic is covered by
tests with a fake client.

Notes that matter for an agent harness:
- Assistant turns are replayed from `provider_content`, the exact blocks the API
  returned (thinking blocks included), so the history stays append-only.
- Prompt caching uses top-level automatic `cache_control`, which caches the
  prefix up to the last cacheable block; the context engine keeps that prefix
  byte-stable.
- Sampling parameters are not sent: current Claude models reject them.
- Tool inputs are validated by the tool runtime, so tools opt into eager input
  streaming.
- Server-side fallbacks (`fallbacks: "default"`) are on by default, so a safety
  classifier's false positive re-runs on Anthropic's recommended fallback model
  instead of stopping the agent. Pass `fallbacks=None` to turn them off.
"""

from __future__ import annotations

import time
from typing import Any, Mapping

from ..util import canonical_json
from .base import ModelError, ModelRequest, ModelResponse
from .pricing import Price, cost_usd

DEFAULT_MODEL = "claude-opus-5-5"


class AnthropicClient:
    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        name: str | None = None,
        effort: str | None = None,
        thinking: Mapping[str, Any] | None = None,
        cache: bool = True,
        fallbacks: str | None = "default",
        max_retries: int = 4,
        timeout: float = 600.0,
        client: Any = None,
        prices: Mapping[str, Price] | None = None,
    ) -> None:
        self.model = model
        self.name = name or f"anthropic:{model}"
        self.effort = effort
        self.thinking = dict(thinking) if thinking else None
        self.cache = cache
        self.fallbacks = fallbacks
        self.prices = prices
        if client is None:
            try:
                import anthropic
            except ImportError as exc:  # pragma: no cover - depends on the environment
                raise ModelError("the anthropic SDK is not installed: pip install 'spool[anthropic]'") from exc
            client = anthropic.AsyncAnthropic(max_retries=max_retries, timeout=timeout)
        self._client = client

    async def aclose(self) -> None:
        close = getattr(self._client, "close", None)
        if close is not None:
            await close()

    def build_params(self, request: ModelRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_tokens,
            "system": request.system,
            "messages": to_anthropic_messages(request.messages),
        }
        if request.tools:
            params["tools"] = [
                {
                    "name": t["name"],
                    "description": t["description"],
                    "input_schema": t["parameters"],
                    "eager_input_streaming": True,
                }
                for t in request.tools
            ]
        if self.cache:
            params["cache_control"] = {"type": "ephemeral"}
        if self.effort:
            params["output_config"] = {"effort": self.effort}
        if self.thinking:
            params["thinking"] = self.thinking
        if self.fallbacks:
            params["fallbacks"] = self.fallbacks
            params["betas"] = ["server-side-fallback-2026-07-01"]
        return params

    async def complete(self, request: ModelRequest) -> ModelResponse:
        params = self.build_params(request)
        started = time.perf_counter()
        try:
            messages_api = self._client.beta.messages if "betas" in params else self._client.messages
            async with messages_api.stream(**params) as stream:
                message = await stream.get_final_message()
        except ValueError as exc:
            # The SDK could not parse a streamed tool input at all; nothing was
            # executed, so the turn can be re-issued.
            raise ModelError(f"unparseable tool input in stream: {exc}", retriable=True) from exc
        except Exception as exc:  # map SDK errors without importing the SDK at module load
            raise _map_sdk_error(exc) from exc
        latency_ms = (time.perf_counter() - started) * 1000
        return self.to_response(message, latency_ms)

    def to_response(self, message: Any, latency_ms: float) -> ModelResponse:
        blocks = [_block_dict(b) for b in message.content]
        text_parts: list[str] = []
        reasoning_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        stop = message.stop_reason or "end_turn"
        for block in blocks:
            kind = block.get("type")
            if kind == "text":
                text_parts.append(block.get("text", ""))
            elif kind == "thinking":
                if block.get("thinking"):
                    reasoning_parts.append(block["thinking"])
            elif kind == "tool_use":
                args = block.get("input")
                error = None
                if not isinstance(args, dict):
                    error, args = "tool input is not a JSON object", {}
                if stop == "max_tokens":
                    error = "the tool input was cut off at max_tokens; send a smaller call"
                tool_calls.append({
                    "id": block["id"],
                    "name": block["name"],
                    "arguments": args,
                    "raw_arguments": canonical_json(args),
                    "parse_error": error,
                })
        u = message.usage
        cache_read = int(getattr(u, "cache_read_input_tokens", 0) or 0)
        cache_write = int(getattr(u, "cache_creation_input_tokens", 0) or 0)
        usage: dict[str, Any] = {
            # The API's input_tokens excludes cached and cache-written tokens; normalize to the total.
            "input_tokens": int(getattr(u, "input_tokens", 0) or 0) + cache_read + cache_write,
            "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
            "cached_tokens": cache_read,
            "cache_write_tokens": cache_write,
        }
        cost, known = cost_usd(self.model, usage, self.prices)
        usage["cost_usd"] = cost
        if not known:
            usage["cost_unknown"] = True
        if stop == "refusal":
            text_parts.append("[The model declined this request.]")
        return ModelResponse(
            content="".join(text_parts).strip(),
            tool_calls=[] if stop == "refusal" else tool_calls,
            stop_reason=stop,
            usage=usage,
            model=getattr(message, "model", self.model) or self.model,
            latency_ms=latency_ms,
            reasoning="\n".join(reasoning_parts) or None,
            provider_content=blocks,
        )


def to_anthropic_messages(messages: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Neutral messages -> Messages API turns.

    Consecutive tool results are merged into one user turn (parallel tool calls
    must be answered together), and consecutive same-role turns are merged.
    """
    out: list[dict[str, Any]] = []

    def push(role: str, blocks: list[dict[str, Any]]) -> None:
        if out and out[-1]["role"] == role:
            out[-1]["content"].extend(blocks)
        else:
            out.append({"role": role, "content": list(blocks)})

    for msg in messages:
        role = msg["role"]
        if role == "user":
            push("user", [{"type": "text", "text": msg.get("content") or "(empty)"}])
        elif role == "assistant":
            if msg.get("provider_content"):
                blocks = [dict(b) for b in msg["provider_content"]]
            else:
                blocks = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for call in msg.get("tool_calls") or []:
                    blocks.append({"type": "tool_use", "id": call["id"], "name": call["name"],
                                   "input": dict(call.get("arguments") or {})})
                if not blocks:
                    blocks.append({"type": "text", "text": "(no output)"})
            push("assistant", blocks)
        elif role == "tool":
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": msg.get("content") or "(no output)",
            }
            if msg.get("is_error"):
                block["is_error"] = True
            push("user", [block])
    return out


def _block_dict(block: Any) -> dict[str, Any]:
    if isinstance(block, dict):
        return dict(block)
    for attr in ("to_dict", "model_dump"):
        fn = getattr(block, attr, None)
        if fn is not None:
            return dict(fn())
    return dict(vars(block))


def _map_sdk_error(exc: Exception) -> ModelError:
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__
    if name in ("APIConnectionError", "APITimeoutError"):
        return ModelError(f"{name}: {exc}", retriable=True)
    if status is not None:
        retry_after = None
        response = getattr(exc, "response", None)
        if response is not None and response.headers.get("retry-after"):
            try:
                retry_after = float(response.headers["retry-after"])
            except ValueError:
                retry_after = None
        return ModelError(
            f"{name} (HTTP {status}): {exc}",
            retriable=status in (408, 409, 429) or status >= 500,
            status=status,
            retry_after=retry_after,
        )
    return ModelError(f"{name}: {exc}", retriable=False)
