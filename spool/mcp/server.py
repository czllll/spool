"""A minimal MCP server over stdio, for the demo services and tests.

It answers both eras: `server/discover` and per-request `_meta` (2026-07-28),
and the `initialize` handshake (2024-11-05 .. 2025-11-25). Only tools are
offered. Requests are handled one at a time, in order, which is all a
single-user local service needs.

    server = Server("mail", instructions="...")

    @server.tool("send_message", "Send an email.", schema, annotations={"openWorldHint": True})
    def send_message(args, meta):
        return "sent"            # or a dict (structured content) or raise ToolError("...")

    server.run_stdio()
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..tools import validate_args
from . import protocol as p

Handler = Callable[[dict[str, Any], dict[str, Any]], Any]


class ToolError(Exception):
    """A failure the model should see (returned as a result with isError), not a protocol error."""


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Handler
    annotations: dict[str, Any] = field(default_factory=dict)

    def describe(self) -> dict[str, Any]:
        out: dict[str, Any] = {"name": self.name, "description": self.description, "inputSchema": self.input_schema}
        if self.annotations:
            out["annotations"] = dict(self.annotations)
        return out


class Server:
    def __init__(self, name: str, version: str = "0.1.0", *, instructions: str | None = None,
                 versions: tuple[str, ...] = p.HANDSHAKE_VERSIONS + p.MODERN_VERSIONS) -> None:
        self.name = name
        self.version = version
        self.instructions = instructions
        self.versions = versions
        self.tools: dict[str, Tool] = {}
        self.era: str | None = None

    def tool(self, name: str, description: str, input_schema: dict[str, Any], *,
             annotations: Mapping[str, Any] | None = None) -> Callable[[Handler], Handler]:
        def register(handler: Handler) -> Handler:
            self.tools[name] = Tool(name, description, input_schema, handler, dict(annotations or {}))
            return handler
        return register

    @property
    def info(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}

    # ------------------------------------------------------------- dispatch
    def handle(self, msg: Mapping[str, Any]) -> dict[str, Any] | None:
        """One incoming message -> the response to write (None for notifications)."""
        method = msg.get("method")
        rid = msg.get("id")
        if not isinstance(method, str):
            return p.error(rid, p.INVALID_REQUEST, "not a request") if rid is not None else None
        if rid is None:
            return None  # notifications (initialized, cancelled, ...) need no answer
        params = dict(msg.get("params") or {})
        requested = (params.get("_meta") or {}).get(p.META_PROTOCOL_VERSION)
        if requested is not None and requested not in self.versions:
            return p.error(rid, p.UNSUPPORTED_PROTOCOL_VERSION, f"unsupported protocol version {requested!r}",
                           {"supported": list(self.versions), "requested": requested})
        modern = requested in p.MODERN_VERSIONS or (requested is None and self.era == "modern")
        # 2026-07-28 list and discover results are cacheable: the client may reuse them for ttlMs.
        cache = {"ttlMs": 0, "cacheScope": "private"} if modern or method == "server/discover" else {}
        try:
            if method == "server/discover":
                modern_versions = [v for v in self.versions if v in p.MODERN_VERSIONS]
                if not modern_versions:
                    return p.error(rid, p.METHOD_NOT_FOUND, "server/discover is not supported")
                self.era = "modern"
                result: dict[str, Any] = {
                    "supportedVersions": modern_versions, "capabilities": {"tools": {}}, "resultType": "complete",
                    "_meta": {p.META_SERVER_INFO: self.info}, **cache,
                }
                if self.instructions:
                    result["instructions"] = self.instructions
                return p.response(rid, result)
            if method == "initialize":
                offered = params.get("protocolVersion")
                handshake = [v for v in self.versions if v in p.HANDSHAKE_VERSIONS]
                if not handshake:
                    return p.error(rid, p.UNSUPPORTED_PROTOCOL_VERSION, "this server only speaks the modern protocol",
                                   {"supported": list(self.versions), "requested": offered})
                self.era = "handshake"
                result = {"protocolVersion": offered if offered in handshake else handshake[-1],
                          "capabilities": {"tools": {}}, "serverInfo": self.info}
                if self.instructions:
                    result["instructions"] = self.instructions
                return p.response(rid, result)
            if method == "ping":
                return p.response(rid, {})
            if method == "tools/list":
                return p.response(rid, {"tools": [t.describe() for t in self.tools.values()], "resultType": "complete",
                                        **cache})
            if method == "tools/call":
                return p.response(rid, self._call(params))
            return p.error(rid, p.METHOD_NOT_FOUND, f"method {method!r} not found")
        except _InvalidParams as exc:
            return p.error(rid, p.INVALID_PARAMS, str(exc))
        except Exception as exc:  # a bug in a handler must not kill the server
            traceback.print_exc(file=sys.stderr)
            return p.error(rid, p.INTERNAL_ERROR, f"{type(exc).__name__}: {exc}")

    def _call(self, params: dict[str, Any]) -> dict[str, Any]:
        tool = self.tools.get(params.get("name") or "")
        if tool is None:
            raise _InvalidParams(f"unknown tool {params.get('name')!r}")
        args = params.get("arguments") or {}
        problems = validate_args(tool.input_schema, args)
        if problems:
            return _result("Invalid arguments: " + "; ".join(problems), error=True)
        try:
            out = tool.handler(dict(args), dict(params.get("_meta") or {}))
        except ToolError as exc:
            return _result(str(exc), error=True)
        if isinstance(out, dict):
            return _result(out.get("text") or json.dumps(out.get("data", out), ensure_ascii=False),
                           structured=out.get("data"))
        return _result(str(out))

    # ---------------------------------------------------------------- stdio
    def run_stdio(self, stdin: Any = None, stdout: Any = None) -> None:
        stdin = stdin or sys.stdin
        stdout = stdout or sys.stdout
        for line in stdin:
            if not line.strip():
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                reply: dict[str, Any] | None = p.error(None, p.PARSE_ERROR, "invalid JSON")
            else:
                reply = self.handle(msg) if isinstance(msg, dict) else p.error(None, p.INVALID_REQUEST, "batches are not supported")
            if reply is not None:
                stdout.write(p.encode(reply).decode("utf-8"))
                stdout.flush()


class _InvalidParams(Exception):
    pass


def _result(text: str, *, error: bool = False, structured: Any = None) -> dict[str, Any]:
    out: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": error, "resultType": "complete"}
    if structured is not None:
        out["structuredContent"] = structured
    return out
