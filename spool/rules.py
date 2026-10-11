"""Standing approval rules: "always allow", "always ask", "never".

    allow  mail__send_message  to=*@mycompany.example   no confirmation for mail to colleagues
    ask    calendar__*                                   ask before any calendar change
    deny   shop__place_order                             never buy anything

Rules belong to the person, not to a session, so they live in their own stream
(`policy:rules`) and apply to every session from the moment they are added.
The policy engine stays pure: it receives the current rules as an argument,
and the POLICY_DECISION it produces records the rule that decided it, so a
replay never re-evaluates anything.

Precedence: deny, then ask, then allow ("ask first" wins over "always allow").
An allow rule only relaxes the per-tool confirmation; it never overrides the
Rule of Two. A standing permission is granted ahead of time, without knowing
what the session will have read by then, which is exactly the case the Rule
of Two exists for.
"""

from __future__ import annotations

import fnmatch
import time
from dataclasses import dataclass, field
from typing import Any, Mapping

from . import events as ev
from .store import EventStore
from .util import new_id

STREAM = ev.POLICY_STREAM_PREFIX + "rules"
EFFECTS = ("deny", "ask", "allow")  # in order of precedence


class RuleError(ValueError):
    pass


@dataclass(frozen=True)
class Rule:
    id: str
    effect: str
    tool: str                                   # glob on the tool name, e.g. "shop__*"
    args: Mapping[str, str] = field(default_factory=dict)  # argument name -> glob on its value
    note: str = ""
    created_at: float = 0.0

    def validate(self) -> None:
        if self.effect not in EFFECTS:
            raise RuleError(f"effect must be one of {EFFECTS}")
        if not self.tool.strip():
            raise RuleError("a rule needs a tool name or pattern, e.g. mail__send_message or shop__*")

    def matches(self, tool: str, arguments: Mapping[str, Any]) -> bool:
        if not fnmatch.fnmatchcase(tool, self.tool):
            return False
        for name, pattern in self.args.items():
            value = arguments.get(name)
            if value is None or not fnmatch.fnmatchcase(str(value).lower(), pattern.lower()):
                return False
        return True

    def describe(self) -> str:
        verb = {"allow": "Always allow", "ask": "Always ask before", "deny": "Never allow"}[self.effect]
        where = "".join(f" where {k} is {v}" for k, v in self.args.items())
        return f"{verb} {self.tool}{where}"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "effect": self.effect, "tool": self.tool, "args": dict(self.args),
                "note": self.note, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "Rule":
        return cls(id=d["id"], effect=d["effect"], tool=d["tool"], args=dict(d.get("args") or {}),
                   note=d.get("note", ""), created_at=float(d.get("created_at") or 0.0))


def first_match(rules: list[Rule] | tuple[Rule, ...], tool: str, arguments: Mapping[str, Any], effect: str) -> Rule | None:
    return next((r for r in rules if r.effect == effect and r.matches(tool, arguments)), None)


class RuleBook:
    """The rules stream, folded incrementally (reads only what was appended since last time)."""

    def __init__(self, store: EventStore, clock: Any = time.time) -> None:
        self.store = store
        self.clock = clock
        self._rules: dict[str, Rule] = {}
        self._next_seq = 0

    def rules(self) -> list[Rule]:
        for e in self.store.load(STREAM, from_seq=self._next_seq):
            if e.type == ev.RULE_ADDED:
                rule = Rule.from_dict(e.data["rule"])
                self._rules[rule.id] = rule
            elif e.type == ev.RULE_REMOVED:
                self._rules.pop(e.data["id"], None)
            self._next_seq = e.seq + 1
        return sorted(self._rules.values(), key=lambda r: (EFFECTS.index(r.effect), r.created_at))

    def add(self, effect: str, tool: str, args: Mapping[str, str] | None = None, note: str = "") -> Rule:
        rule = Rule(new_id("rule"), effect, tool.strip(), dict(args or {}), note, self.clock())
        rule.validate()
        self.rules()
        self.store.append(STREAM, ev.RULE_ADDED, {"rule": rule.to_dict()}, expected_seq=self._next_seq)
        return rule

    def remove(self, rule_id: str) -> None:
        if rule_id not in {r.id for r in self.rules()}:
            raise KeyError(f"no rule {rule_id!r}")
        self.store.append(STREAM, ev.RULE_REMOVED, {"id": rule_id}, expected_seq=self._next_seq)
