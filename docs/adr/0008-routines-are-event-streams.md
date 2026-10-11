# ADR 0008: Routines are event streams; unattended approvals expire

Status: accepted

## Context

Background agents (Grok Bot, Muse, Manus, Qwen's task assistant) start work on their own: on a
schedule, when something arrives, or when another system calls them. They work while nobody is
watching and come back only when an action needs approval. That raises questions a chat session
never has to answer:

- If the scheduler crashes between "it is 09:00" and "the run has started", does the run happen
  zero, one or two times? What if two schedulers are running?
- What happens to the 09:00 run when the machine was off from 08:00 to 10:00?
- Should a second run start while the first is still waiting for approval?
- An approval requested at 03:00 by a background run: can it be granted at 15:00? Grok Bot's
  documentation answers "no": approvals from routines and triggers expire after about ten
  minutes and do not run. A stale "allow" for an action whose context has changed is a risk,
  not a convenience.

## Decision

1. **A routine is a stream in the same event store** (`routine:<name>`): `ROUTINE_SAVED`,
   `ROUTINE_FIRED`, `ROUTINE_SKIPPED`, `ROUTINE_PAUSED`, `ROUTINE_RESUMED`, `ROUTINE_DELETED`.
   `reduce_routine` folds it; `due(state, now, files)` is pure and returns the fires that are due.
2. **Write-ahead firing.** `ROUTINE_FIRED` names the session it is about to start and is appended
   with the stream's expected sequence number before the session starts.
   - Two schedulers racing on one routine: the second append fails on the sequence check, the
     loser re-reads and sees the time handled. One run.
   - A crash between the append and the session start: the next tick finds a fired session that
     does not exist and starts it. The session id is derived from the fire (`r-<name>-<n>`), and
     starting a session is itself a sequence-checked append, so it can never start twice.
3. **Triggers.** Cron (five fields, Vixie day-of-month/day-of-week OR semantics, wall-clock time in
   the routine's time zone; times skipped by a forward clock change do not fire, repeated ones fire
   once), interval, new files in a folder of the routine's workspace (new paths only, so a run
   that edits its inputs does not trigger itself), and webhooks with an optional delivery key.
4. **Policies.** `misfire = run_once` (one catch-up run for the latest missed time) or `skip`
   (record the miss); `overlap = skip` (do not start while the previous run is running, sleeping
   or waiting for approval; recorded as `ROUTINE_SKIPPED`) or `allow`. A webhook that arrives
   while the routine is busy is not recorded at all: the sender gets HTTP 409 with Retry-After and
   retries with the same key, so the event is neither lost nor run twice.
5. **Approvals can expire** (`approval_ttl_s`; routine runs default to 600 s). The deadline is
   written into `APPROVAL_REQUESTED`; `decide(state, now)` returns `ExpireApproval` once it has
   passed, and the runtime records a denial `by: expired`. An "allow" that arrives after the
   deadline is refused, and the expiry is recorded by whichever observer sees the deadline first.
   The model is told the action did not run and should say what is waiting.
6. **Finding due work is one indexed query**: the sessions projection keeps `due_at` (the wake-up
   time of a sleeping session or the deadline of a pending approval). `spool tick` (cron-friendly),
   `spool daemon` and the web server's scheduler loop all run the same `Scheduler.tick()` plus
   `store.due_sessions(now)`.

## Consequences

- "Why did this run start?" and "why did the 09:00 run not happen?" have answers in the log.
- Scheduling is testable without sleeping: every rule above is a function of (log, now, files)
  and is tested with a fake clock (`tests/test_routines.py`, `tests/test_approval_expiry.py`).
- The scheduler is single-node. Several schedulers on one store are safe (the sequence checks
  make firing exactly-once), but running sessions is not yet leased: two processes could both
  resume the same interrupted session. Leases are the next step for multi-worker execution.
- Replaying a long-lived routine stream gets slower with every fire; the scheduler keeps the
  folded state in memory and only reads new events, and a snapshot would be the next step.
