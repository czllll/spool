"""Protocol constants and helpers shared by the client and the server."""

from __future__ import annotations

import json
from typing import Any, Mapping

JSONRPC = "2.0"

HANDSHAKE_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25")
MODERN_VERSIONS = ("2026-07-28",)
LATEST_HANDSHAKE = HANDSHAKE_VERSIONS[-1]
LATEST_MODERN = MODERN_VERSIONS[-1]

# _meta keys of the 2026-07-28 per-request envelope
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

# JSON-RPC and MCP error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
REQUEST_TIMEOUT = -32001
UNSUPPORTED_PROTOCOL_VERSION = -32022

# Methods that must not be cancelled (the spec forbids cancelling negotiation)
UNCANCELLABLE = ("initialize", "server/discover")


def request(id: int | str, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC, "id": id, "method": method}
    if params is not None:
        msg["params"] = dict(params)
    return msg


def notification(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    msg: dict[str, Any] = {"jsonrpc": JSONRPC, "method": method}
    if params is not None:
        msg["params"] = dict(params)
    return msg


def response(id: int | str | None, result: Any) -> dict[str, Any]:
    return {"jsonrpc": JSONRPC, "id": id, "result": result}


def error(id: int | str | None, code: int, message: str, data: Any = None) -> dict[str, Any]:
    err: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    return {"jsonrpc": JSONRPC, "id": id, "error": err}


def encode(msg: Mapping[str, Any]) -> bytes:
    """One message per line (stdio framing): JSON with no embedded newlines."""
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


def result_text(result: Mapping[str, Any]) -> str:
    """The text a model should see for a `tools/call` result.

    Text blocks are joined; other blocks are named rather than dropped silently; a result
    with only structured content is shown as JSON.
    """
    parts: list[str] = []
    for block in result.get("content") or ():
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind in ("image", "audio"):
            size = len(block.get("data") or "") * 3 // 4
            parts.append(f"[{kind}: {block.get('mimeType', 'unknown type')}, about {size} bytes]")
        elif kind == "resource_link":
            parts.append(f"[resource: {block.get('uri')}{' (' + block['name'] + ')' if block.get('name') else ''}]")
        elif kind == "resource":
            res = block.get("resource") or {}
            parts.append(res["text"] if "text" in res else f"[binary resource: {res.get('uri')}]")
        else:
            parts.append(f"[unsupported content block: {kind}]")
    if not parts and result.get("structuredContent") is not None:
        parts.append(json.dumps(result["structuredContent"], ensure_ascii=False, indent=2))
    return "\n".join(parts)
