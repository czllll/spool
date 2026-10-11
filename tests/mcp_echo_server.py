"""Test MCP server built on spool.mcp.server. Usage: mcp_echo_server.py [modern|handshake|both]"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spool.mcp import protocol as p  # noqa: E402
from spool.mcp.server import Server, ToolError  # noqa: E402

eras = sys.argv[1] if len(sys.argv) > 1 else "both"
versions = {"modern": p.MODERN_VERSIONS, "handshake": p.HANDSHAKE_VERSIONS,
            "both": p.HANDSHAKE_VERSIONS + p.MODERN_VERSIONS}[eras]
server = Server("echo", instructions="Echoes things back.", versions=versions)
S = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}


@server.tool("echo", "Echo the text.", S, annotations={"readOnlyHint": True, "openWorldHint": False})
def echo(args, meta):
    return args["text"]


@server.tool("add", "Add two integers.", {"type": "object", "properties": {"a": {"type": "integer"},
                                                                          "b": {"type": "integer"}},
                                          "required": ["a", "b"]})
def add(args, meta):
    return {"text": str(args["a"] + args["b"]), "data": {"sum": args["a"] + args["b"]}}


@server.tool("fail", "Always fails.", {"type": "object", "properties": {}})
def fail(args, meta):
    raise ToolError("this tool always fails")


@server.tool("slow", "Sleep.", {"type": "object", "properties": {"seconds": {"type": "number"}}})
def slow(args, meta):
    time.sleep(float(args.get("seconds", 1)))
    return "slept"


@server.tool("crash", "Exit the server.", {"type": "object", "properties": {}})
def crash(args, meta):
    sys.stderr.write("crashing on purpose\n")
    sys.stderr.flush()
    os._exit(3)


@server.tool("meta", "Return the request _meta keys.", {"type": "object", "properties": {}})
def meta(args, meta):
    return ",".join(sorted(meta))


server.run_stdio()
