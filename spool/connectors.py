"""Connectors: MCP servers whose tools the agent can call.

    # <data-dir>/connectors.toml
    [connector.mail]
    command = ["{python}", "-m", "spool.services.mail", "--db", "{state_dir}/mail.sqlite"]
    scope = "session"        # one server per session (state under its ledger dir); "shared": one for all
    sensitive = true         # its data is private: reading it counts as sensitive data (Rule of Two)
    [connector.mail.tools.send_message]
    confirm = true           # per-tool settings override what the server's annotations suggest

A session enables connectors by name (`AgentConfig.connectors`). Before its
first model call the runtime lists their tools and records the result in the
log (a NOTE of kind "connector_tools"): the exact schemas, the policy derived
for each tool, and the server's identity. From then on the session's tools and
its system prompt are a function of the log, like everything else: a server
that changes its tools mid-session does not change the prompt prefix, the
policy, or what a replay renders.

Each server tool becomes a Spool tool named `<connector>__<tool>`:
  properties  read-only tools: untrusted input (if open-world), sensitive data (if
              the connector is sensitive); tools that change something: external_comm
              if they reach an open world or destroy data. An additive write inside the
              user's own data (mark a message read, add a calendar entry) cannot send
              anything anywhere, so it does not count toward the Rule of Two
  confirm     tools that change something outside a closed world, or destroy data,
              ask for approval every time
  crash       read-only and idempotent tools re-run after a crash; a tool whose
              schema has an `idempotency_key` argument is *ledgered*: Spool hides that
              argument from the model, fills it with the call's idempotency key, and
              re-sends the same key after a crash, so the server can return the first
              result instead of acting twice; any other tool is reported as
              interrupted ("may or may not have happened")
MCP annotations are hints, and missing ones mean the cautious default (may
write, may destroy, open world); per-tool settings in the config always win.
"""

from __future__ import annotations

import asyncio
import copy
import os
import re
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from .mcp import MCPClient, MCPError, result_text
from .tools import PROP_EXTERNAL, PROP_SENSITIVE, PROP_UNTRUSTED, ToolContext, ToolRegistry, ToolResult, ToolSpec
from .tools.builtin import DEFAULT_TOOLS

IDEM_ARG = "idempotency_key"
SCOPES = ("shared", "session")
TOOL_OVERRIDES = ("confirm", "properties", "idempotent", "hidden")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,30}$")


class ConnectorError(ValueError):
    pass


@dataclass(frozen=True)
class ConnectorSpec:
    name: str
    command: tuple[str, ...]
    env: Mapping[str, str] = field(default_factory=dict)
    cwd: str | None = None
    scope: str = "shared"
    protocol: str = "auto"
    sensitive: bool = False
    timeout_s: float = 60.0
    description: str = ""
    tools: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    here: str | None = None  # directory of the file it was loaded from ({here} in command, env, cwd)

    def validate(self) -> None:
        if not NAME_RE.match(self.name):
            raise ConnectorError(f"connector names use lowercase letters, digits and _ ({self.name!r})")
        if not self.command:
            raise ConnectorError(f"connector {self.name!r} needs a command")
        if self.scope not in SCOPES:
            raise ConnectorError(f"connector {self.name!r}: scope must be one of {SCOPES}")
        if self.protocol not in ("auto", "handshake", "modern"):
            raise ConnectorError(f"connector {self.name!r}: protocol must be auto, handshake or modern")
        for tool, settings in self.tools.items():
            unknown = set(settings) - set(TOOL_OVERRIDES)
            if unknown:
                raise ConnectorError(f"connector {self.name!r}, tool {tool!r}: unknown settings {sorted(unknown)}")

    def expand(self, values: Mapping[str, str]) -> tuple[list[str], dict[str, str], str | None]:
        values = {"here": self.here or os.getcwd(), **values}

        def fill(text: str) -> str:
            return re.sub(r"\{(\w+)\}", lambda m: values.get(m.group(1), m.group(0)), text)
        return [fill(c) for c in self.command], {k: fill(v) for k, v in self.env.items()}, fill(self.cwd) if self.cwd else None


def load_connectors(path: str | Path) -> dict[str, ConnectorSpec]:
    """`[connector.<name>]` tables of a TOML file; relative `{here}` paths resolve against the file."""
    path = Path(path)
    with open(path, "rb") as fh:
        raw = tomllib.load(fh)
    out: dict[str, ConnectorSpec] = {}
    for name, table in (raw.get("connector") or {}).items():
        table = dict(table)
        unknown = set(table) - {"command", "env", "cwd", "scope", "protocol", "sensitive", "timeout_s", "description", "tools"}
        if unknown:
            raise ConnectorError(f"connector {name!r}: unknown keys {sorted(unknown)}")
        spec = ConnectorSpec(
            name=name, command=tuple(table.get("command") or ()), env=dict(table.get("env") or {}),
            cwd=table.get("cwd"), scope=table.get("scope", "shared"), protocol=table.get("protocol", "auto"),
            sensitive=bool(table.get("sensitive", False)), timeout_s=float(table.get("timeout_s", 60.0)),
            description=table.get("description", ""), tools={k: dict(v) for k, v in (table.get("tools") or {}).items()},
            here=str(path.resolve().parent),
        )
        spec.validate()
        out[name] = spec
    return out


# ------------------------------------------------------------ pure: tools
def tool_name(connector: str, tool: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", f"{connector}__{tool}")[:64]


def tool_entry(spec: ConnectorSpec, tool: Mapping[str, Any]) -> dict[str, Any]:
    """What the session records about one server tool: its model-facing schema and its derived policy."""
    ann = dict(tool.get("annotations") or {})
    override = dict(spec.tools.get(tool["name"]) or {})
    read_only = bool(ann.get("readOnlyHint", False))
    destructive = bool(ann.get("destructiveHint", True))
    open_world = bool(ann.get("openWorldHint", True))
    schema = copy.deepcopy(dict(tool.get("inputSchema") or {"type": "object", "properties": {}}))
    props = schema.get("properties") or {}
    idem_arg = IDEM_ARG if IDEM_ARG in props else None
    if idem_arg:  # Spool fills it; the model never sees it
        props.pop(idem_arg)
        schema["required"] = [r for r in schema.get("required") or [] if r != idem_arg]
    if read_only:
        properties = {PROP_UNTRUSTED} if open_world else set()
        if spec.sensitive:
            properties.add(PROP_SENSITIVE)
    else:
        properties = {PROP_EXTERNAL} if (open_world or destructive) else set()
    if "properties" in override:
        properties = set(override["properties"])
    confirm = bool(override.get("confirm", (not read_only) and (destructive or open_world)))
    idempotent = bool(override.get("idempotent", read_only or bool(ann.get("idempotentHint", False))))
    description = (tool.get("description") or "").strip()
    if confirm:
        description = (description + " " if description else "") + "The user must approve each call."
    return {
        "name": tool_name(spec.name, tool["name"]),
        "connector": spec.name,
        "tool": tool["name"],
        "description": description,
        "parameters": schema,
        "properties": sorted(properties),
        "confirm": confirm,
        "idempotent": idempotent,
        "ledgered": idem_arg is not None and not idempotent,
        "idem_arg": idem_arg,
        "annotations": ann,
    }


def connector_specs(entries: list[Mapping[str, Any]], handler_for: Any = None) -> list[ToolSpec]:
    return [
        ToolSpec(
            name=e["name"], description=e["description"], parameters=dict(e["parameters"]),
            handler=handler_for(e) if handler_for else _unavailable,
            properties=frozenset(e["properties"]), idempotent=bool(e["idempotent"]),
            ledgered=bool(e["ledgered"]), confirm=bool(e["confirm"]),
        )
        for e in entries
    ]


def session_registry(state: Any, builtin: ToolRegistry, handler_for: Any = None) -> ToolRegistry:
    """The tools a session can call: its built-ins, plus the connector tools recorded in its log."""
    cfg = state.config
    base = builtin.subset(cfg.tools if cfg.tools is not None else DEFAULT_TOOLS)
    if not state.connector_tools:
        return base
    return base.merged(connector_specs([dict(e) for e in state.connector_tools], handler_for))


async def _unavailable(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    return ToolResult.fail("this connector is not available in this process")


# --------------------------------------------------------- the live side
class ConnectorManager:
    """Starts connector servers on demand and calls their tools."""

    def __init__(self, specs: Mapping[str, ConnectorSpec], *, data_dir: str | Path) -> None:
        self.specs = dict(specs)
        self.data_dir = Path(data_dir)
        self._clients: dict[str, MCPClient] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_file(cls, path: str | Path | None, *, data_dir: str | Path) -> "ConnectorManager | None":
        if path is None or not Path(path).exists():
            return None
        return cls(load_connectors(path), data_dir=data_dir)

    @staticmethod
    def default_path(data_dir: str | Path, explicit: str | None = None) -> Path | None:
        """--connectors, else $SPOOL_CONNECTORS, else <data-dir>/connectors.toml (if it exists)."""
        for candidate in (explicit, os.environ.get("SPOOL_CONNECTORS"), str(Path(data_dir) / "connectors.toml")):
            if candidate:
                path = Path(candidate).expanduser()
                if explicit and candidate == explicit and not path.exists():
                    raise ConnectorError(f"connectors file {candidate} does not exist")
                if path.exists():
                    return path
        return None

    def names(self) -> list[str]:
        return sorted(self.specs)

    def _key(self, spec: ConnectorSpec, state_dir: Path) -> str:
        return spec.name if spec.scope == "shared" else f"{spec.name}@{state_dir}"

    async def client(self, name: str, *, workspace: Path, state_dir: Path) -> MCPClient:
        spec = self.specs.get(name)
        if spec is None:
            raise MCPError(f"connector {name!r} is not configured")
        key = self._key(spec, state_dir)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            current = self._clients.get(key)
            if current is not None and current.loop is not asyncio.get_running_loop():
                current.abandon()  # started under an event loop that has since ended
                current = None
            if current is not None and current.alive:
                return current
            if current is not None:
                await current.close()
            state = state_dir if spec.scope == "session" else self.data_dir / "services"
            state.mkdir(parents=True, exist_ok=True)
            command, env, cwd = spec.expand({
                "python": sys.executable, "data_dir": str(self.data_dir.resolve()), "state_dir": str(state.resolve()),
                "workspace": str(workspace), "home": os.path.expanduser("~"),
            })
            client = MCPClient(command, env=env, cwd=cwd, name=name, protocol=spec.protocol, timeout_s=spec.timeout_s)
            await client.start()
            self._clients[key] = client
            return client

    async def snapshot(self, names: list[str] | tuple[str, ...], *, workspace: Path, state_dir: Path) -> dict[str, Any]:
        """List the tools of `names`; the result is appended to the session as a NOTE."""
        tools: list[dict[str, Any]] = []
        servers: dict[str, Any] = {}
        errors: dict[str, str] = {}
        for name in names:
            spec = self.specs.get(name)
            if spec is None:
                errors[name] = "not configured"
                continue
            try:
                client = await self.client(name, workspace=workspace, state_dir=state_dir)
                listed = await client.list_tools()
            except (MCPError, OSError) as exc:
                errors[name] = str(exc)
                continue
            servers[name] = {"era": client.era, "version": client.version, "server": client.server_info,
                             "instructions": client.instructions or spec.description or None}
            for tool in listed:
                if not (spec.tools.get(tool.get("name", "")) or {}).get("hidden"):
                    tools.append(tool_entry(spec, tool))
        return {"kind": "connector_tools", "tools": sorted(tools, key=lambda t: t["name"]),
                "servers": servers, "errors": errors}

    def handler_for(self, entry: Mapping[str, Any]):
        async def call(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
            name = entry["connector"]
            try:
                client = await self.client(name, workspace=ctx.workspace, state_dir=ctx.ledger_dir)
                if entry.get("idem_arg"):
                    args = {**args, entry["idem_arg"]: ctx.idem_key}
                result = await client.call_tool(entry["tool"], args, timeout=ctx.config.tool_timeout_s)
            except MCPError as exc:
                return ToolResult.fail(f"The {name} connector failed: {exc}")
            text = result_text(result)
            meta: dict[str, Any] = {"connector": name}
            structured = result.get("structuredContent")
            if isinstance(structured, dict) and structured.get("deduplicated"):
                meta["deduplicated"] = True
            if result.get("isError"):
                return ToolResult.fail(text or f"{entry['tool']} reported an error", output=text, meta=meta)
            return ToolResult(ok=True, output=text, data={"meta": meta})
        return call

    async def release(self, state_dir: Path) -> None:
        """Stop the session-scoped servers of one session (their state stays on disk)."""
        for key in [k for k in self._clients if k.endswith(f"@{state_dir}")]:
            await self._stop(self._clients.pop(key))

    async def aclose(self) -> None:
        clients, self._clients = list(self._clients.values()), {}
        for client in clients:
            await self._stop(client)

    @staticmethod
    async def _stop(client: MCPClient) -> None:
        if client.loop is asyncio.get_running_loop():
            await client.close()
        else:
            client.abandon()
