"""A deterministic model for tests: plays back a script of responses."""

from __future__ import annotations

import dataclasses
from typing import Any, Callable, Union

from ..util import canonical_json
from .base import ModelError, ModelRequest, ModelResponse

ScriptItem = Union[ModelResponse, dict, Callable[[ModelRequest], Any], Exception]


def reply(text: str, *, input_tokens: int = 100, output_tokens: int = 20, cached_tokens: int = 0) -> ModelResponse:
    return ModelResponse(
        content=text,
        tool_calls=[],
        stop_reason="end_turn",
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached_tokens,
               "cache_write_tokens": 0, "cost_usd": 0.0},
        model="scripted",
        latency_ms=1.0,
    )


def call(name: str, call_id: str | None = None, text: str = "", **arguments: Any) -> ModelResponse:
    return calls([(name, arguments, call_id)], text=text)


def calls(items: list[tuple[str, dict[str, Any], str | None]], *, text: str = "",
          input_tokens: int = 100, output_tokens: int = 20, cached_tokens: int = 0) -> ModelResponse:
    tool_calls = []
    for i, (name, arguments, call_id) in enumerate(items):
        tool_calls.append({
            "id": call_id or f"call_{name}_{i}",
            "name": name,
            "arguments": arguments,
            "raw_arguments": canonical_json(arguments),
            "parse_error": None,
        })
    return ModelResponse(
        content=text,
        tool_calls=tool_calls,
        stop_reason="tool_use",
        usage={"input_tokens": input_tokens, "output_tokens": output_tokens, "cached_tokens": cached_tokens,
               "cache_write_tokens": 0, "cost_usd": 0.0},
        model="scripted",
        latency_ms=1.0,
    )


def bad_call(name: str, raw: str, call_id: str = "call_bad") -> ModelResponse:
    resp = calls([(name, {}, call_id)])
    resp.tool_calls[0]["raw_arguments"] = raw
    resp.tool_calls[0]["parse_error"] = "arguments are not valid JSON"
    return resp


class ByStepModel:
    """Answers request k (k = assistant turns already in the request + 1) with script[k-1].

    Unlike ScriptedModel it keeps no cursor, so a fresh process resuming a
    crashed session gets the right next response. Used by crash tests.
    """

    def __init__(self, script: list[ModelResponse], name: str = "by-step") -> None:
        self.script = script
        self.name = name
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        step = sum(1 for m in request.messages if m["role"] == "assistant")
        if step >= len(self.script):
            raise ModelError(f"by-step model: no response scripted for step {step + 1}", retriable=False)
        item = self.script[step]
        return dataclasses.replace(item, tool_calls=[dict(c) for c in item.tool_calls])

    async def aclose(self) -> None:
        return None


class ScriptedModel:
    """Returns scripted responses in order and records every request it saw.

    A script item may be a ModelResponse, a dict of ModelResponse fields, an
    exception to raise, or a callable taking the request (for responses that
    depend on what the agent sent).
    """

    def __init__(self, script: list[ScriptItem], name: str = "scripted") -> None:
        self.script = list(script)
        self.name = name
        self.requests: list[ModelRequest] = []

    async def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if not self.script:
            raise ModelError("scripted model: script exhausted", retriable=False)
        item = self.script.pop(0)
        if callable(item) and not isinstance(item, (ModelResponse, dict)):
            item = item(request)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, dict):
            item = ModelResponse(**item)
        return dataclasses.replace(item, tool_calls=[dict(c) for c in item.tool_calls])

    async def aclose(self) -> None:
        return None
