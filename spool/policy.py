"""Policy engine: tool risk properties and Meta's "Agents Rule of Two".

Until prompt injection can be reliably detected, a session should not combine
all three of: [A] processing untrusted input, [B] accessing sensitive data,
[C] changing state externally or communicating out. The engine tracks which
of these a session has already exercised (its taint, folded from the log) and
holds back any call that would complete the triple:
  approval=human        -> ask: the session suspends until someone decides
  approval=auto_deny    -> deny: the model is told why and carries on
  approval=auto_approve -> allow (for measuring what the policy prevents)

`evaluate` is pure; the runtime records its verdict as a POLICY_DECISION event,
so audits and replays see exactly what was decided and why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .state import State, ToolCall
from .tools import ALL_PROPERTIES, PROP_EXTERNAL, PROP_SENSITIVE, PROP_UNTRUSTED, ToolSpec, classify_path

_PHRASES = {
    PROP_UNTRUSTED: "processed untrusted content",
    PROP_SENSITIVE: "accessed sensitive data",
    PROP_EXTERNAL: "communicated externally",
}
_VERB = {
    PROP_UNTRUSTED: "process untrusted content",
    PROP_SENSITIVE: "access sensitive data",
    PROP_EXTERNAL: "communicate externally",
}
_MODE_VERDICT = {"auto_approve": "allow", "auto_deny": "deny", "human": "ask"}


def call_properties(spec: ToolSpec, args: dict[str, Any], state: State, isolation: str) -> frozenset[str]:
    props = set(spec.properties)
    if spec.path_arg and spec.path_reads:
        path = args.get(spec.path_arg) or "."
        if isinstance(path, str):
            props |= classify_path(Path(state.workspace), path, state.config)
    if spec.name == "bash" and isolation == "process" and state.config.sensitive_paths:
        # Without OS-level isolation a shell can read the protected paths directly,
        # so every command has to be treated as a potential sensitive read.
        props.add(PROP_SENSITIVE)
    return frozenset(props)


def evaluate(call: ToolCall, spec: ToolSpec, state: State, *, isolation: str) -> dict[str, Any]:
    cfg = state.config
    props = call_properties(spec, dict(call.args), state, isolation)
    decision: dict[str, Any] = {
        "call_id": call.id,
        "tool": call.name,
        "properties": sorted(props),
        "verdict": "allow",
        "rule": "default_allow",
        "reason": "",
    }
    if cfg.policy == "off":
        decision["rule"] = "policy_off"
        return decision
    combined = state.taint | props
    if props and all(p in combined for p in ALL_PROPERTIES):
        held = [p for p in ALL_PROPERTIES if p in state.taint]
        adds = [p for p in ALL_PROPERTIES if p in props and p not in state.taint] or sorted(props)
        reason = (
            "Blocked by the Rule of Two: this session has already "
            + (" and ".join(_PHRASES[p] for p in held) if held else "no prior risk")
            + f"; '{call.name}' would also "
            + " and ".join(_VERB[p] for p in adds)
            + ". Doing all three in one session needs human approval. Do not retry this action; "
              "continue without it, or finish and say what approval is needed."
        )
        decision.update(verdict=_MODE_VERDICT[cfg.approval], rule="rule_of_two", reason=reason)
    return decision
