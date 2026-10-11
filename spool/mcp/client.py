"""An MCP client over stdio.

One server process per client. A reader task demultiplexes the server's stdout:
responses resolve the pending request with the same id, server requests get an
answer (`ping`; everything else is "method not found", since this client
declares no client capabilities), and notifications are recorded. stderr is
kept in a small ring buffer for error messages.

Negotiation (`protocol="auto"`), as in the official SDKs:
  1. send `server/discover` at the newest modern version;
     - a result listing a modern version we speak  -> modern era;
     - -32022 naming a modern version we speak     -> discover again at that version;
     - -32022 naming only modern versions we do not speak -> incompatible;
     - anything else (method not found, timeout, a result with no modern version)
                                                   -> fall back to the handshake;
  2. `initialize` at the newest handshake version; the server's answer must be a
     version we speak; then `notifications/initialized`. A -32022 here means the
     server already locked in a modern era, and is answered with one more discover.
In the modern era every request carries `_meta` with the protocol version, the
client info and the (empty) client capabilities.

Timeouts send `notifications/cancelled` for the request (except negotiation,
which must not be cancelled). If the process dies, every pending request fails
and the next call raises; the owner decides whether to start a new client.
"""

from __future__ import annotations

import asyncio
import collections
import json
import os
import signal
from typing import Any, Mapping, Sequence

from . import protocol as p

CLIENT_INFO = {"name": "spool", "version": "0.2.0"}
DISCOVER_TIMEOUT_S = 5.0
READ_LIMIT = 32 * 1024 * 1024  # one JSON-RPC message per line; tool results can be large


class MCPError(Exception):
    def __init__(self, message: str, code: int | None = None, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data


class MCPClient:
    def __init__(
        self,
        command: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        cwd: str | None = None,
        name: str = "server",
        protocol: str = "auto",
        timeout_s: float = 60.0,
    ) -> None:
        if protocol not in ("auto", "handshake", "modern"):
            raise ValueError("protocol must be auto, handshake or modern")
        self.command = list(command)
        self.env = dict(env or {})
        self.cwd = cwd
        self.name = name
        self.protocol = protocol
        self.timeout_s = timeout_s
        self.era: str | None = None          # "handshake" | "modern" once negotiated
        self.version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.instructions: str | None = None
        self.capabilities: dict[str, Any] = {}
        self.tools_changed = False
        self.log: collections.deque[dict[str, Any]] = collections.deque(maxlen=50)
        self.stderr: collections.deque[str] = collections.deque(maxlen=50)
        self._proc: asyncio.subprocess.Process | None = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._tasks: list[asyncio.Task] = []
        self._write_lock = asyncio.Lock()
        self._closed_reason: str | None = None
        self.loop: asyncio.AbstractEventLoop | None = None

    # ------------------------------------------------------------ lifecycle
    @property
    def alive(self) -> bool:
        return self._proc is not None and self._proc.returncode is None and self._closed_reason is None

    async def start(self) -> "MCPClient":
        self.loop = asyncio.get_running_loop()
        env = {**os.environ, **self.env}
        self._proc = await asyncio.create_subprocess_exec(
            *self.command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, env=env, cwd=self.cwd, limit=READ_LIMIT)
        self._tasks = [asyncio.create_task(self._read_stdout()), asyncio.create_task(self._read_stderr())]
        try:
            await self._negotiate()
        except BaseException:
            await self.close()
            raise
        return self

    async def close(self) -> None:
        proc, self._proc = self._proc, None
        self._fail_pending("client closed")
        if proc is not None and proc.returncode is None:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, ProcessLookupError, ConnectionResetError, BrokenPipeError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()
        # Let the readers see EOF so asyncio closes the pipes now, not in a destructor later.
        tasks, self._tasks = self._tasks, []
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=1.0)
            for task in pending:
                task.cancel()

    def abandon(self) -> None:
        """Stop the server without the event loop (the loop that started it is gone)."""
        proc, self._proc = self._proc, None
        self._closed_reason = "abandoned"
        if proc is not None and proc.returncode is None:
            try:
                os.kill(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    # ----------------------------------------------------------- negotiation
    async def _negotiate(self) -> None:
        if self.protocol == "handshake":
            await self._handshake()
            return
        version = p.LATEST_MODERN
        for attempt in range(2):
            try:
                result = await self._discover(version)
            except MCPError as exc:
                if exc.code == p.UNSUPPORTED_PROTOCOL_VERSION:
                    supported = _supported(exc.data)
                    mutual = [v for v in p.MODERN_VERSIONS if v in supported]
                    if mutual and attempt == 0:
                        version = mutual[-1]
                        continue
                    if supported and not any(v in p.HANDSHAKE_VERSIONS for v in supported):
                        raise MCPError(f"{self.name}: no common protocol version (server speaks {supported})",
                                       exc.code, exc.data) from None
                if self.protocol == "modern":
                    raise
                if await self._handshake_or_reprobe(attempt):
                    return
                version = self._reprobe_version
                continue
            mutual = [v for v in p.MODERN_VERSIONS if v in (result.get("supportedVersions") or ())]
            if not mutual:  # answers discover but advertises only handshake versions: a legacy server
                if self.protocol == "modern":
                    raise MCPError(f"{self.name}: server does not speak {p.LATEST_MODERN}")
                await self._handshake()
                return
            self.era, self.version = "modern", mutual[-1]
            self.capabilities = result.get("capabilities") or {}
            self.instructions = result.get("instructions")
            self.server_info = ((result.get("_meta") or {}).get(p.META_SERVER_INFO)) or {}
            return
        raise MCPError(f"{self.name}: protocol negotiation failed")

    async def _discover(self, version: str) -> dict[str, Any]:
        meta = {p.META_PROTOCOL_VERSION: version, p.META_CLIENT_INFO: CLIENT_INFO, p.META_CLIENT_CAPABILITIES: {}}
        return await self._request("server/discover", {"_meta": meta}, timeout=DISCOVER_TIMEOUT_S, stamp=False)

    async def _handshake_or_reprobe(self, attempt: int) -> bool:
        """Run the handshake; False (with _reprobe_version set) if the server answered -32022."""
        try:
            await self._handshake()
            return True
        except MCPError as exc:
            if exc.code != p.UNSUPPORTED_PROTOCOL_VERSION or attempt != 0:
                raise
            mutual = [v for v in p.MODERN_VERSIONS if v in _supported(exc.data)]
            if not mutual:
                raise
            self._reprobe_version = mutual[-1]
            return False

    async def _handshake(self) -> None:
        result = await self._request("initialize", {
            "protocolVersion": p.LATEST_HANDSHAKE, "capabilities": {}, "clientInfo": CLIENT_INFO,
        }, stamp=False)
        version = result.get("protocolVersion")
        if version not in p.HANDSHAKE_VERSIONS:
            raise MCPError(f"{self.name}: server chose unsupported protocol version {version!r}")
        self.era, self.version = "handshake", version
        self.capabilities = result.get("capabilities") or {}
        self.instructions = result.get("instructions")
        self.server_info = result.get("serverInfo") or {}
        await self._send(p.notification("notifications/initialized"))

    # -------------------------------------------------------------- requests
    async def list_tools(self) -> list[dict[str, Any]]:
        tools: list[dict[str, Any]] = []
        cursor = None
        for _ in range(100):  # pagination guard
            params = {"cursor": cursor} if cursor else {}
            result = await self._request("tools/list", params)
            tools.extend(result.get("tools") or [])
            cursor = result.get("nextCursor")
            if not cursor:
                break
        self.tools_changed = False
        return tools

    async def call_tool(self, name: str, arguments: Mapping[str, Any], *, timeout: float | None = None,
                        meta: Mapping[str, Any] | None = None) -> dict[str, Any]:
        params: dict[str, Any] = {"name": name, "arguments": dict(arguments)}
        if meta:
            params["_meta"] = dict(meta)
        result = await self._request("tools/call", params, timeout=timeout)
        if result.get("resultType", "complete") != "complete":
            raise MCPError(f"{self.name}: tool {name} needs input this client cannot provide "
                           f"(resultType {result.get('resultType')!r})")
        return result

    async def _request(self, method: str, params: dict[str, Any], *, timeout: float | None = None,
                       stamp: bool = True) -> dict[str, Any]:
        if not self.alive:
            raise MCPError(f"{self.name}: server is not running ({self._closed_reason or 'not started'})"
                           + self._stderr_tail())
        if stamp and self.era == "modern":
            meta = params.setdefault("_meta", {})
            meta[p.META_PROTOCOL_VERSION] = self.version
            meta[p.META_CLIENT_INFO] = CLIENT_INFO
            meta[p.META_CLIENT_CAPABILITIES] = {}
        self._next_id += 1
        rid = self._next_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = future
        try:
            await self._send(p.request(rid, method, params))
            return await asyncio.wait_for(future, timeout=timeout or self.timeout_s)
        except asyncio.TimeoutError:
            if method not in p.UNCANCELLABLE and self.alive:
                await self._send(p.notification("notifications/cancelled",
                                                {"requestId": rid, "reason": "timed out"}))
            raise MCPError(f"{self.name}: {method} timed out after {timeout or self.timeout_s:g}s",
                           p.REQUEST_TIMEOUT) from None
        finally:
            self._pending.pop(rid, None)

    async def _send(self, msg: Mapping[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            raise MCPError(f"{self.name}: server is not running")
        async with self._write_lock:
            try:
                proc.stdin.write(p.encode(msg))
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError) as exc:
                raise MCPError(f"{self.name}: server closed its input ({exc})" + self._stderr_tail()) from None

    # ---------------------------------------------------------------- reader
    async def _read_stdout(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:  # a single line above READ_LIMIT
                    self._closed_reason = "server sent a message larger than the read limit"
                    break
                if not line:
                    break
                if not line.strip():
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    self.log.append({"level": "warning", "data": f"non-JSON line on stdout: {line[:200]!r}"})
                    continue
                await self._dispatch(msg)
        finally:
            code = proc.returncode
            if code is None:
                try:  # stdout is closed; give the process a moment to exit, but never hang here
                    code = await asyncio.wait_for(proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    code = None
            self._closed_reason = self._closed_reason or (
                f"server exited with code {code}" if code is not None else "server closed its output")
            self._fail_pending(self._closed_reason)

    async def _read_stderr(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stderr is not None
        while True:
            line = await proc.stderr.readline()
            if not line:
                return
            self.stderr.append(line.decode("utf-8", "replace").rstrip())

    async def _dispatch(self, msg: dict[str, Any]) -> None:
        if "method" in msg:
            if "id" in msg:  # a request from the server
                if msg["method"] == "ping":
                    await self._send(p.response(msg["id"], {}))
                else:
                    await self._send(p.error(msg["id"], p.METHOD_NOT_FOUND, f"client does not support {msg['method']}"))
            elif msg["method"] == "notifications/tools/list_changed":
                self.tools_changed = True
            elif msg["method"] == "notifications/message":
                self.log.append(dict(msg.get("params") or {}))
            return
        future = self._pending.get(msg.get("id"))  # type: ignore[arg-type]
        if future is None or future.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            future.set_exception(MCPError(f"{self.name}: {err.get('message', 'error')}", err.get("code"), err.get("data")))
        else:
            future.set_result(msg.get("result") or {})

    def _fail_pending(self, reason: str) -> None:
        for future in self._pending.values():
            if not future.done():
                future.set_exception(MCPError(f"{self.name}: {reason}" + self._stderr_tail()))
        self._pending.clear()

    def _stderr_tail(self) -> str:
        tail = [line for line in list(self.stderr)[-3:] if line]
        return (" (stderr: " + " | ".join(tail)[:400] + ")") if tail else ""


def _supported(data: Any) -> list[str]:
    if isinstance(data, dict) and isinstance(data.get("supported"), list):
        return [str(v) for v in data["supported"]]
    return []
