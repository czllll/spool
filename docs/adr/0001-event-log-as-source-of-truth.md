# ADR 0001: The session is an append-only event log

Status: accepted

## Context

Always-on agents (OpenAI Dots, xAI Grok Bot, Meta's Muse agent, Manus Cue) run for hours or days.
In that time the process will restart, a human will take hours to approve something, and someone
will ask "why did it do that?". Most agent frameworks keep the conversation as an in-memory list
of messages, so a crash loses the task and a bad trajectory cannot be reproduced.

## Decision

Every session is an ordered list of immutable events in SQLite (`spool/store.py`). Each event has a
dense per-session sequence number; an append names the sequence number it expects, so two writers
racing on one session cannot both succeed. The runtime appends an event *before* the effect it
describes becomes visible (see ADR 0007). State, model context, metrics and reports are all derived
from the log.

## Alternatives considered

- **Mutable conversation snapshot** (what most frameworks do). Simple, but recovery needs bespoke
  checkpoints, and history edits are invisible.
- **A workflow engine (Temporal, Restate, DBOS).** Gives durable execution for free, but adds a
  second system to run, and its history is about activities, not about what the model saw. For a
  single-node runtime the log plus a pure reducer gives the same guarantees in ~300 lines.
- **JSONL files per session.** No atomic multi-row writes, no cheap listing/indexing, and fsync
  discipline is easy to get wrong.

## Consequences

- Crash recovery, replay, forking, audit and evals read the same data; there is no separate
  "trace" format to keep in sync.
- The log only grows, which is exactly the shape a KV/prompt cache wants (ADR 0003).
- `synchronous=FULL` makes each append an fsync. Measured on an M1 laptop (`scripts/bench_store.py`,
  2 KB events): p50 0.33 ms, p99 7 ms, against p50 0.20 ms / p99 1.4 ms with `NORMAL`. Either is
  noise next to a model call (seconds). A `kill -9` cannot lose a committed event in either mode;
  `FULL` also covers an OS crash. On macOS a power cut additionally needs `PRAGMA fullfsync=ON`,
  because plain fsync does not flush the drive's own cache.
- The `sessions` table is a projection for listing and can be rebuilt from `events`.

## Revisit when

Many writers per session (multi-agent fan-in) or many nodes: move to Postgres with the same
`(session_id, seq)` primary key, or put the log behind a single-writer lease.
