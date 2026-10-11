# ADR 0004: Tool policy based on Meta's Agents Rule of Two

Status: accepted

## Context

There is no reliable defense against prompt injection: "The Attacker Moves Second" (arXiv,
2025-10-10) broke most published defenses with adaptive attacks. Meta's "Agents Rule of Two"
(2025-10-31) proposes a structural rule instead: within one session an agent should not combine
all three of [A] processing untrusted input, [B] accessing sensitive data, and [C] changing state
externally or communicating out, unless a human approves.

## Decision

- Each tool declares static properties (`web_fetch`: A and C; `send_message`: C), and path tools
  are classified per call (`read_file secrets/...`: B; `read_file downloads/...`: A).
- The session's taint (properties already exercised) is folded from the log. A sub-agent's taint
  flows back to its parent with its answer.
- `policy.evaluate` is pure. If a call would complete the triple it is held back: `ask` (session
  suspends until `spool approve`), `deny` (the model is told why), or `allow` (to measure the
  policy). The verdict, rule and properties are recorded as a `POLICY_DECISION` event.
- The sandbox makes the rule enforceable: protected paths are unreadable from `bash` (Seatbelt
  deny rules, or an empty tmpfs mounted over them in Docker), so the policy-mediated `read_file`
  is the only way to reach them. With the plain `process` backend there is no such guarantee, so
  every `bash` call counts as a sensitive read.

## Consequences

- Legitimate tasks that need all three (read a token, then call an external endpoint after reading
  untrusted content) need a human in the loop by design.
- The policy limits the blast radius of an injection; it does not detect one.
- Coverage is deterministic: `tests/test_policy.py` and `tests/test_runtime.py` drive scripted
  trajectories through every combination. A model-driven injection benchmark is not part of this
  repository.
