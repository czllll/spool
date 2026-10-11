"""MCP client and server: both protocol eras, errors, timeouts, crashes, and interop with the official SDK."""

import asyncio
import sys
from pathlib import Path

import pytest

from spool.mcp import MCPClient, MCPError, result_text
from spool.mcp import protocol as p
from spool.mcp.server import Server

from .conftest import run

HERE = Path(__file__).resolve().parent
ECHO = [sys.executable, str(HERE / "mcp_echo_server.py")]


def client(eras="both", **kw) -> MCPClient:
    return MCPClient(ECHO + [eras], name="echo", **kw)


# ------------------------------------------------------------ server unit
def test_server_answers_both_eras():
    server = Server("t", instructions="hi")
    discover = server.handle(p.request(1, "server/discover", {"_meta": {p.META_PROTOCOL_VERSION: "2026-07-28"}}))
    assert discover["result"]["supportedVersions"] == ["2026-07-28"]
    assert discover["result"]["_meta"][p.META_SERVER_INFO]["name"] == "t"
    init = server.handle(p.request(2, "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}))
    assert init["result"]["protocolVersion"] == "2025-06-18"  # an offered version it speaks is echoed
    newer = server.handle(p.request(3, "initialize", {"protocolVersion": "2030-01-01"}))
    assert newer["result"]["protocolVersion"] == p.LATEST_HANDSHAKE  # otherwise its latest
    bad = server.handle(p.request(4, "tools/list", {"_meta": {p.META_PROTOCOL_VERSION: "1999-01-01"}}))
    assert bad["error"]["code"] == p.UNSUPPORTED_PROTOCOL_VERSION and "2026-07-28" in bad["error"]["data"]["supported"]
    assert server.handle(p.request(5, "nope"))["error"]["code"] == p.METHOD_NOT_FOUND
    assert server.handle(p.notification("notifications/initialized")) is None


def test_result_text_renders_every_block_kind():
    result = {"content": [{"type": "text", "text": "a"}, {"type": "image", "mimeType": "image/png", "data": "AAAA"},
                          {"type": "resource_link", "uri": "file:///x", "name": "x"},
                          {"type": "resource", "resource": {"uri": "u", "text": "inline"}}]}
    assert result_text(result) == "a\n[image: image/png, about 3 bytes]\n[resource: file:///x (x)]\ninline"
    assert result_text({"content": [], "structuredContent": {"n": 1}}) == '{\n  "n": 1\n}'


# ------------------------------------------------------------ over stdio
def test_auto_negotiates_modern_and_stamps_meta():
    async def go():
        c = await client().start()
        try:
            assert (c.era, c.version) == ("modern", "2026-07-28")
            assert c.server_info["name"] == "echo" and c.instructions == "Echoes things back."
            names = [t["name"] for t in await c.list_tools()]
            assert names[:3] == ["echo", "add", "fail"]
            assert result_text(await c.call_tool("echo", {"text": "hi"})) == "hi"
            added = await c.call_tool("add", {"a": 2, "b": 3})
            assert added["structuredContent"] == {"sum": 5}
            failed = await c.call_tool("fail", {})
            assert failed["isError"] and "always fails" in result_text(failed)
            invalid = await c.call_tool("add", {"a": "x", "b": 1})
            assert invalid["isError"] and "must be of type integer" in result_text(invalid)
            meta = result_text(await c.call_tool("meta", {}))
            assert p.META_PROTOCOL_VERSION in meta and p.META_CLIENT_INFO in meta
            with pytest.raises(MCPError) as err:
                await c.call_tool("missing", {})
            assert err.value.code == p.INVALID_PARAMS
        finally:
            await c.close()
    run(go())


def test_auto_falls_back_to_the_handshake():
    async def go():
        c = await client("handshake").start()
        try:
            assert (c.era, c.version) == ("handshake", p.LATEST_HANDSHAKE)
            assert result_text(await c.call_tool("echo", {"text": "old"})) == "old"
            assert result_text(await c.call_tool("meta", {})) == ""  # no per-request envelope in this era
        finally:
            await c.close()
    run(go())


def test_forced_eras():
    async def go():
        with pytest.raises(MCPError, match="only speaks the modern protocol"):
            await client("modern", protocol="handshake").start()
        with pytest.raises(MCPError):
            await client("handshake", protocol="modern").start()
        c = await client("both", protocol="handshake").start()
        assert c.era == "handshake"
        await c.close()
    run(go())


def test_timeout_and_crash():
    async def go():
        c = await client(timeout_s=0.5).start()
        try:
            with pytest.raises(MCPError) as err:
                await c.call_tool("slow", {"seconds": 2})
            assert err.value.code == p.REQUEST_TIMEOUT
            with pytest.raises(MCPError, match="exited with code 3|closed"):
                await c.call_tool("crash", {}, timeout=5)
            await asyncio.sleep(0.1)
            assert not c.alive
            with pytest.raises(MCPError, match="not running.*crashing on purpose"):
                await c.call_tool("echo", {"text": "x"})
        finally:
            await c.close()
    run(go())


def test_missing_command_fails_cleanly():
    async def go():
        with pytest.raises(FileNotFoundError):
            await MCPClient(["/nonexistent/server"]).start()
    run(go())


# ------------------------------------------------------- official SDK
def test_spool_client_talks_to_an_sdk_server():
    pytest.importorskip("mcp.server.mcpserver")

    async def go():
        for protocol, era in (("auto", "modern"), ("handshake", "handshake")):
            c = await MCPClient([sys.executable, str(HERE / "mcp_sdk_server.py")], name="sdk", protocol=protocol).start()
            try:
                assert c.era == era
                tools = await c.list_tools()
                assert [t["name"] for t in tools] == ["multiply"]
                assert result_text(await c.call_tool("multiply", {"a": 6, "b": 7})) == "42"
            finally:
                await c.close()
    run(go())


def test_sdk_client_talks_to_a_spool_server():
    pytest.importorskip("mcp.client.client")
    from mcp.client.client import Client
    from mcp.client.stdio import StdioServerParameters

    async def go():
        for mode in ("auto", "legacy"):
            params = StdioServerParameters(command=sys.executable, args=[str(HERE / "mcp_echo_server.py"), "both"])
            async with Client(params, mode=mode) as sdk:
                listed = await sdk.list_tools()
                assert "echo" in [t.name for t in listed.tools]
                result = await sdk.call_tool("add", {"a": 20, "b": 22})
                assert result.content[0].text == "42" and result.structured_content == {"sum": 42}
    run(go())
