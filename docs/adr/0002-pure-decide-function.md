# ADR 0002: The agent loop is a pure state machine

Status: accepted

## Context

Agent loops are usually written as one async function that calls the model, runs tools, catches
errors and mutates a list. Such a loop is hard to test without a model and hard to resume halfway.

## Decision

Split the loop into a functional core and an imperative shell:

- `reduce(state, event) -> state` (`spool/state.py`): folds events into bounded control-flow state.
  No IO, no clock, no randomness.
- `decide(state, now) -> action` (`spool/decide.py`): names the next action (call the model,
  authorize a call, run a tool, wait for approval, sleep, finish). `now` is an explicit input.
- `Runtime.run` (`spool/runtime.py`): performs the action and appends the resulting events.

## Consequences

- Recovery has no special code path: replay the log, call `decide`. A `TOOL_STARTED` without a
  `TOOL_COMPLETED` makes `decide` return `ExecuteTool(recovering=True)`.
- The decision table is unit-tested from hand-written event lists (`tests/test_state_decide.py`),
  and `replay(store.load(id)) == runtime.state(id)` is asserted after real runs.
- Policy verdicts are computed by a pure function too and recorded as events, so a later change to
  the policy code does not change how an old session replays.

## Alternatives considered

- A single async loop with try/except around each step: fewer files, but every new failure mode
  becomes another branch in the same function, and testing needs a model or heavy mocking.
