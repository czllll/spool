# ADR 0009: Connectors are MCP servers; their tools are snapshotted into the log

Status: accepted

## Context

An agent that does things for a person works on that person's services: mail, calendar,
shopping, payments. In 2026 the common way to reach them is the Model Context Protocol, and the
protocol itself changed this year: 2026-07-28 replaced the `initialize` handshake with a
stateless envelope (every request carries its protocol version, client info and capabilities in
`_meta`; `server/discover` advertises versions). Servers of both eras are in use.

Three properties of the runtime have to survive this:

- **Replay and caching**: rendering is a pure function of the log. A server is a live input; if
  its tool list changed mid-session, the prompt prefix (cache) and the policy would change with it.
- **Policy**: the Rule of Two needs to know, per tool, whether it reads untrusted content, reads
  private data, or acts outside. MCP tool annotations (`readOnlyHint`, `destructiveHint`,
  `idempotentHint`, `openWorldHint`) are hints, and the spec says not to trust them from untrusted
  servers.
- **Exactly-once writes** (ADR 0007): a remote write interrupted by a crash must not be repeated.

## Decision

1. **A small client and server written against the spec** (`spool/mcp/`), not the SDK: one stdio
   process per server, a reader task demultiplexing responses, server requests and
   notifications, timeouts that send `notifications/cancelled`. Negotiation follows the official
   SDKs: probe `server/discover`, adopt the modern era on positive evidence, otherwise fall back to
   `initialize`. Interop is tested both ways against the official Python SDK (its client against
   Spool's server, Spool's client against its server, in both eras). Writing it found one spec
   detail the first version missed: 2026-07-28 list and discover results must carry `ttlMs` and
   `cacheScope`.
2. **Snapshot before the first model call.** The runtime lists the session's connector tools and
   appends them as a `NOTE` (`connector_tools`): model-facing schema, derived policy, server
   identity and instructions. Tools, the "Connected services" part of the system prompt and the
   policy are read from the log afterwards. A server that changes later affects new sessions only.
   `spool verify` re-renders connector sessions without any server running.
3. **Policy derived from annotations, overridden by config.**
   - read-only tools: untrusted input if open-world; sensitive data if the connector is marked
     sensitive;
   - writes: external communication if they reach an open world or destroy data; an additive
     write inside the person's own data (mark read, add a calendar entry) cannot leak anything
     and does not count;
   - `confirm` (ask every time, whatever the session has done): open-world or destructive writes;
   - missing annotations mean the cautious default (may write, may destroy, open world).
   Per-tool settings in `connectors.toml` (`confirm`, `properties`, `idempotent`, `hidden`) win.
   `spool connectors` prints the result for every tool.
4. **Exactly-once remote writes need the other side.** A tool whose input schema has an
   `idempotency_key` property is *ledgered*: Spool removes that property from what the model
   sees, fills it with the call's key (`<session>:<call_id>`), and after a crash sends the same
   key again; the server returns its first result. Read-only and idempotent tools are re-run;
   anything else is reported as interrupted ("may or may not have happened").
5. **Scope.** A connector is `shared` (one server process for the runtime: a person's real
   accounts) or `session` (one per session, its state under the session's ledger dir: what the
   evals use, so every trial gets fresh services).

## Consequences

- The demo services (mail, calendar, shop) are ordinary MCP servers; any stdio MCP server can be
  added the same way.
- The evals can check the runtime from the outside: after each trial the services' own records are
  matched against the log (writes without approval, after expiry, without a key from the log, and
  duplicates). See `spool/services/audit.py` and the actions suite.
- Not done: Streamable HTTP transport and OAuth (a hosted server would need both), MCP resources
  and prompts, and the 2026 "input required" round trip (treated as an error).
