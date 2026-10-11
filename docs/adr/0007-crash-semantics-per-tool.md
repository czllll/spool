# ADR 0007: Crash semantics are declared per tool

Status: accepted

## Context

A process can die at any instruction. The dangerous window is "the effect happened, its result was
not yet recorded": re-running blindly can send an email twice; skipping can lose work.

## Decision

The runtime writes `TOOL_STARTED` (with idempotency key `<session>:<call_id>`) before running a
tool and `TOOL_COMPLETED` after. On resume, an open call is handled according to the tool's
declaration:

| tool kind | examples | on resume |
|---|---|---|
| idempotent | read_file, write_file, edit_file (detects "already applied"), list_files | run again |
| ledgered | send_message, web_fetch log | run again with the same key; the ledger (fsynced JSONL under an exclusive lock, outside the workspace) returns the first result |
| neither | bash | not re-run; the model is told the command was interrupted and its effects are unknown |

Sub-agents get a deterministic session id (`<parent>.<call_id>`), so re-running the spawn call
resumes the same child instead of starting a second one.

## Consequences

- Exactly-once external effects without distributed transactions, as long as the external system
  is reached only through ledgered tools.
- Tested with simulated crashes at every point (`tests/test_crash_recovery.py`) and with real
  `SIGKILL` of the agent process followed by a resume in a new process (unit test and the crash
  eval in `scripts/run_evals.sh`).
