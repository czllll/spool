# Spool design

This document describes how Spool works inside. The ADRs in `docs/adr/` record why each choice was
made and what was rejected.

## 1. The loop

```
             ┌──────────────── Runtime.run(session) ────────────────┐
  events ──► │ replay → State ──► decide(State, now) ──► Action      │
  (SQLite)   │                                            │          │
     ▲       │   CallModel ─ render → gateway → MODEL_RESPONSE      │
     │       │   Authorize ─ validate args → policy → POLICY_DECISION│
     │       │   ExecuteTool ─ TOOL_STARTED → sandbox → TOOL_COMPLETED│
     │       │   RequestApproval / Sleep / Wake / Finish            │
     └───────┴──────────── append (fsync) ◄────────────────────────┘
```

`decide` is pure (`spool/decide.py`). The runtime (`spool/runtime.py`) performs exactly one action
per iteration and appends the events it produced; the in-memory `State` is advanced by the same
`reduce` that rebuilds it after a restart.

## 2. Events

| event | written when | key fields |
|---|---|---|
| `session_started` | `Runtime.start` / `fork` | task, frozen config, workspace, parent pointer, depth, fixtures |
| `user_message` | task or follow-up | text |
| `model_response` | after each model call | message (content, tool_calls with raw argument strings, provider blocks), usage incl. cached tokens, latency, server timings, per-part request digests, render timestamp |
| `model_error` | a call failed after retries | error, retriable, status |
| `policy_decision` | before a tool runs | verdict (allow/deny/ask), rule, Rule-of-Two properties, reason |
| `approval_requested` / `approval_decided` | `ask` verdicts | call id, rule, reason, deadline (`expires_at`); approver, or `expired` |
| `tool_started` | before the tool runs | arguments, idempotency key, call signature |
| `tool_completed` | after the tool (or instead of it) | ok, kind (executed, deduplicated, invalid, denied, rejected, interrupted), output, offload path and preview, loop warning, plan reminder, todos, wake time, propagated taint |
| `context_folded` | fold strategy, over budget | through_step |
| `context_compacted` | summarize strategy, over budget | upto_seq, summary, usage |
| `session_finished` | terminal | status (completed, failed, max_steps, budget_exhausted), answer, usage |
| `note` | bookkeeping | woke, recovery, `connector_tools` (the connector tool snapshot, section 13) |

Routine streams (`routine:<name>`, section 12) have their own events: `routine_saved`,
`routine_fired` (fire key, scheduled time, payload, the session it starts), `routine_skipped`,
`routine_paused`, `routine_resumed`, `routine_deleted`.

## 3. Crash semantics

See ADR 0007. In short: idempotent tools re-run; ledgered tools re-run with the same idempotency key
and get the recorded result; `bash` is reported as interrupted. A model call in flight is simply
re-issued, since nothing was appended for it.

Crash points used by tests and evals (`SPOOL_CRASH_AT=point[:tool][:n]`):
`before_model_call`, `after_model_response`, `after_tool_started`, `after_tool_executed`.

## 4. Context engine

Rendering order: system prompt, tool schemas sorted by name, then messages in event order.

| strategy | large tool outputs | when over budget | prefix |
|---|---|---|---|
| `full` | inline, capped at `hard_cap_chars` (head and tail kept) | nothing | append-only |
| `offload` (default) | file under `.spool/outputs/`, preview inline | nothing | append-only |
| `fold` | as offload | outputs older than `keep_recent_steps` become stubs; `recall(call_id)` restores | rewritten once per fold |
| `summarize` | inline, capped | older turns replaced by a model-written summary | rewritten once per compaction |

Two more things are appended, never inserted: a loop warning when the same call (same name and
arguments) appears `loop_threshold` times in the last `loop_window` calls, and every `recite_every`
tool calls a restatement of the current plan from `todo_write`. Both are decided at completion time
and stored in the event, so rendering stays a function of the log.

`spool verify <session>` re-renders every request and compares digests with the recorded ones.

## 5. Model gateway

Neutral messages are OpenAI-chat shaped; adapters translate.

- `OpenAICompatClient`: hand-written SSE parser; tool-call deltas keyed by index with arguments
  arriving in fragments; tolerant argument parsing; recovery of `<tool_call>` blocks leaked into
  text by local chat templates; usage normalized to total prompt tokens plus cached tokens (from
  `prompt_tokens_details` or llama.cpp `timings.cache_n`); retries on 408/409/429/5xx and transport
  errors with capped exponential backoff and full jitter, honoring `retry-after`; optional token
  bucket; optional slot pinning (`id_slot`) for session affinity.
- The assistant's tool-call arguments are sent back as the exact string the model produced, not
  re-serialized, so the server sees the bytes it already has in its cache.
- `AnthropicClient` (official SDK, optional extra): replays assistant turns from the exact blocks
  returned (thinking blocks included), uses top-level automatic prompt caching, sends no sampling
  parameters, validates tool inputs itself.
- `ScriptedModel`, `ByStepModel`, `RecordingModel` / `ReplayModel` for tests and offline replay.

Truncated calls (`stop_reason == max_tokens`) are never executed; the model is told to retry
smaller.

## 6. Policy

See ADR 0004. Properties per call: static per tool, plus path classification for reading tools
(`sensitive_paths`, `untrusted_paths`). Taint is the union of properties of calls that actually ran
(denied calls do not taint). Unknown tools and schema-invalid arguments are rejected before policy,
with errors phrased for the model.

Two rules, in order: `rule_of_two` (the call would complete the triple) and `confirm` (the tool
asks every time: connector tools that send, buy or delete). Both map to a verdict through
`approval` (human: ask, auto_deny: deny, auto_approve: allow). With `approval_ttl_s`, an `ask`
carries a deadline; `decide` expires it (section 12).

## 7. Sandboxes

See ADR 0005. The runtime asks the sandbox for its isolation level; the policy treats unsandboxed
`bash` as a sensitive read.

## 8. Sub-agents and sleeping

- `spawn_subagent(task, max_steps)` starts a child session `<parent>.<call_id>` in the same
  workspace with a fresh context and without the spawn tool (depth 1). The parent receives only the
  child's final answer; the child's taint and usage roll up.
- `sleep(seconds)` records a wake time; `run` returns `sleeping`; `spool tick` (cron-friendly)
  resumes every due session.

## 9. Evals

See ADR 0006. `spool eval` runs tasks x trials with one server slot pinned per concurrent trial,
appends one JSON line per trial, and renders `report.md`. Crash mode drives the session through
`spool resume` subprocesses, the first of which is SIGKILLed at the configured point.

## 10. RL and data

- `SpoolEnv` (`spool/env.py`): `reset()` returns the request the harness would send, `step(action)`
  takes one assistant turn and returns the next request; the reward is the task verifier. Tools,
  policy, sandbox and logging are the real ones.
- `spool export` writes passed trajectories as chat-format JSONL with tool schemas, for
  rejection-sampling fine-tuning.

## 11. Web app

`spool web` serves a Starlette app (`spool/web/app.py`) and one static page (`spool/web/static/`,
plain HTML/CSS/JS, no build step).

- **Runs** are background asyncio tasks, one per session at a time (`SessionHub`). Starting a task,
  sending a follow-up, approving, branching and resuming are each "append an event, then run".
- **Live view = the log.** `GET /api/sessions/{id}/events` is Server-Sent Events. The handler
  subscribes first, replays every event after `Last-Event-ID` from SQLite, then forwards new events
  as the runtime appends them (each SSE `id` is the event's seq). A browser reconnect, or a server
  restart, resumes the stream exactly where it stopped; the client also drops any seq it has seen.
- **Approvals** come from the `approval_requested` event: the page shows Allow / Deny, the POST
  appends `approval_decided`, and the session continues from the same step.
- **Stop** cancels the run task without writing anything; the session then shows as interrupted
  and Resume continues from the last recorded event, the same path as crash recovery.
- **Folder** browsing goes through the same path jail as the file tools, and protected directories
  are not listed.
- **Routines** and **Approvals** pages: the routines with their triggers, next run and recent runs
  (run now, pause, edit, delete); every pending approval across sessions, with its deadline.
- Unless read-only, the server runs the scheduler (section 12) every `tick_s` seconds and resumes
  routine runs that a previous server left mid-way.
- Static assets are linked with a content hash, so a browser never runs an old script against a
  newer server.
- The server binds to 127.0.0.1 and has no authentication: it is a local tool.

## 12. Routines and the scheduler

See ADR 0008. `spool/routines.py`:

```
 tick(now) ──► for each routine: replay stream ─► due(state, now, files) ─► fires
                 fire ─► append ROUTINE_FIRED (expected seq; names session r-<name>-<n>)
                      ─► Runtime.start(session)  (a crash in between is repaired next tick)
           ──► store.due_sessions(now)  (wake-ups and lapsed approvals, one indexed query)
           ──► run them (web server, `spool daemon`, or `spool tick` from cron)
```

| trigger | fires | key |
|---|---|---|
| cron | wall-clock schedule in the routine's time zone | `cron<i>:<scheduled time>` |
| interval | every N seconds from when the routine was armed | `interval<i>:<scheduled time>` |
| file | new paths matching a glob in a folder of the routine workspace | digest of the new files |
| webhook | `POST /api/routines/<name>/fire` (JSON body = `{payload}`) | `Idempotency-Key` header |

Runs use the routine's persistent workspace, render `{date} {time} {now} {files} {payload}` into
the task, and default to `approval = human` with `approval_ttl_s = 600`.

## 13. Connectors

See ADR 0009. `spool/mcp/` (protocol), `spool/connectors.py` (tools and policy),
`spool/services/` (demo servers and the audit).

```
first model call ─► ConnectorManager.snapshot(cfg.connectors) ─► NOTE connector_tools
                    (tools/list on each server; schema minus idempotency_key; derived policy)
later            ─► session_registry(state) = built-ins + tools from the NOTE
call             ─► policy (rule_of_two, confirm) ─► TOOL_STARTED ─► tools/call with the
                    call's idempotency key filled in ─► TOOL_COMPLETED
crash            ─► ledgered: same key again (server dedupes); idempotent: re-run;
                    otherwise: reported as interrupted
```

`spool connectors` prints, per tool, the Rule-of-Two properties, whether it asks every time, and
its crash behaviour.
