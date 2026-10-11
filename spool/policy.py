"""Policy engine: tool risk properties and Meta's "Agents Rule of Two".

Two rules, checked in this order:

1. Rule of Two. Until prompt injection can be reliably detected, a session should not combine
all three of: [A] processing untrusted input, [B] accessing sensitive data,
[C] changing state externally or communicating out. The engine tracks which
of these a session has already exercised (its taint, folded from the log) and
holds back any call that would complete the triple:
  approval=human        -> ask: the session suspends until someone decides
  approval=auto_deny    -> deny: the model is told why and carries on
  approval=auto_approve -> allow (for measuring what the policy prevents)

2. Confirm. Tools marked `confirm` (connector tools that send, buy or delete) ask
   every time, whatever the session has done before; same mapping to verdicts.

Standing rules (spool.rules) come first and last: a matching "deny" rule blocks
the call outright, a matching "ask" rule asks even where nothing else would, and
a matching "allow" rule removes the confirmation (but never a Rule-of-Two ask).

`evaluate` is pure; the runtime records its verdict as a POLICY_DECISION event,
so audits and replays see exactly what was decided and why.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

from .rules import Rule, first_match
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


def evaluate(call: ToolCall, spec: ToolSpec, state: State, *, isolation: str,
             rules: Sequence[Rule] = ()) -> dict[str, Any]:
    cfg = state.config
    args = dict(call.args)
    props = call_properties(spec, args, state, isolation)
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
    denied = first_match(rules, call.name, args, "deny")
    if denied is not None:
        decision.update(verdict="deny", rule="user_rule", user_rule=denied.to_dict(),
                        reason=f"The user has a standing rule against this ({denied.describe()}). Do not retry it; "
                               "continue without it, or finish and say what you could not do.")
        return decision
    ask = first_match(rules, call.name, args, "ask")
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
    elif ask is not None:
        decision.update(verdict=_MODE_VERDICT[cfg.approval], rule="user_rule", user_rule=ask.to_dict(),
                        reason=f"The user asked to approve these calls ({ask.describe()}). If it is not approved, "
                               "do not retry it; finish and say what is waiting for approval.")
    elif spec.confirm:
        allowed = first_match(rules, call.name, args, "allow")
        if allowed is not None:
            decision.update(rule="user_rule", user_rule=allowed.to_dict())
        else:
            reason = (f"'{call.name}' acts outside the workspace, so every call needs the user's approval. "
                      "If it is not approved, do not retry it; finish and say what is waiting for approval.")
            decision.update(verdict=_MODE_VERDICT[cfg.approval], rule="confirm", reason=reason)
    return decision
