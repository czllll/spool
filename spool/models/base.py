"""Provider-neutral request/response types for the model gateway.

The neutral message format is OpenAI-chat-like:
  {"role": "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [{"id", "name", "arguments", "raw_arguments"}],
   "provider_content": [...]}            # optional: exact provider blocks to replay (e.g. thinking)
  {"role": "tool", "tool_call_id": str, "name": str, "content": str, "is_error": bool}
Adapters translate this to each provider's wire format.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from ..util import digest


@dataclass
class ModelRequest:
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]]
    max_tokens: int = 2048
    temperature: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)  # not part of the digest

    def body(self) -> dict[str, Any]:
        return {
            "system": self.system,
            "messages": self.messages,
            "tools": self.tools,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
        }

    def digest(self) -> str:
        return digest(self.body())

    def digest_parts(self) -> dict[str, Any]:
        """Short per-part hashes, stored with every response.

        Comparing consecutive requests' parts shows exactly where a prefix broke
        (system, tools, or message i) without storing the requests twice.
        """
        return {
            "digest": self.digest()[:16],
            "system": digest(self.system, 12),
            "tools": digest(self.tools, 12),
            "messages": [digest(m, 12) for m in self.messages],
        }


@dataclass
class ModelResponse:
    content: str
    tool_calls: list[dict[str, Any]]
    stop_reason: str
    usage: dict[str, Any]
    model: str
    latency_ms: float
    reasoning: str | None = None
    provider_content: list[dict[str, Any]] | None = None
    timings: dict[str, Any] | None = None

    def to_event_data(self) -> dict[str, Any]:
        message: dict[str, Any] = {"content": self.content, "tool_calls": self.tool_calls}
        if self.reasoning:
            message["reasoning"] = self.reasoning
        if self.provider_content is not None:
            message["provider_content"] = self.provider_content
        data: dict[str, Any] = {
            "message": message,
            "usage": self.usage,
            "model": self.model,
            "latency_ms": round(self.latency_ms, 1),
            "stop_reason": self.stop_reason,
        }
        if self.timings:
            data["timings"] = self.timings
        return data


class ModelError(Exception):
    def __init__(self, message: str, *, retriable: bool = False, status: int | None = None,
                 retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retriable = retriable
        self.status = status
        self.retry_after = retry_after


class ModelClient(Protocol):
    name: str

    async def complete(self, request: ModelRequest) -> ModelResponse: ...

    async def aclose(self) -> None: ...


class PinnedModel:
    """Tags every request with a server slot (session affinity for prefix caches).

    The slot travels in request metadata, which is not part of the request digest,
    so pinning never changes what the model sees.
    """

    def __init__(self, inner: ModelClient, slot: int) -> None:
        self.inner = inner
        self.slot = slot
        self.name = inner.name

    async def complete(self, request: ModelRequest) -> ModelResponse:
        request.metadata["slot"] = self.slot
        return await self.inner.complete(request)

    async def aclose(self) -> None:  # the shared inner client is closed by its owner
        return None


def parse_arguments(raw: str | None) -> tuple[dict[str, Any], str | None]:
    """Parse a tool call's JSON arguments; never raises.

    Small models often wrap JSON in code fences or emit an empty string for a
    no-argument call; both are accepted. Anything else that does not parse to
    an object is reported back to the model as an invalid call.
    """
    if raw is None or not raw.strip():
        return {}, None
    text = raw.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        return {}, f"arguments are not valid JSON ({exc.msg} at position {exc.pos})"
    if not isinstance(value, dict):
        return {}, f"arguments must be a JSON object, got {type(value).__name__}"
    return value, None
