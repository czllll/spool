"""Session state as a fold over events.

`reduce(state, event) -> state` is a pure function: no IO, no clock, no
randomness. The runtime keeps one State per open session and advances it with
every event it appends; recovery after a crash is `replay(store.load(id))`.

State holds only bounded control-flow data (pending calls, counters, the
current plan). The full history stays in the event list, which the context
engine reads directly.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Iterable, Mapping

from . import events as ev
from .config import AgentConfig
from .events import Event

EXECUTED_KINDS = ("executed", "interrupted", "deduplicated")


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    args: Mapping[str, Any] = field(default_factory=dict)
    raw_args: str | None = None
    parse_error: str | None = None

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "ToolCall":
        return cls(
            id=d["id"],
            name=d["name"],
            args=d.get("arguments") or {},
            raw_args=d.get("raw_arguments"),
            parse_error=d.get("parse_error"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "arguments": dict(self.args),
            "raw_arguments": self.raw_args,
            "parse_error": self.parse_error,
        }


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    model_calls: int = 0
    model_latency_ms: float = 0.0

    def add(self, usage: Mapping[str, Any], latency_ms: float = 0.0) -> "Usage":
        return Usage(
            input_tokens=self.input_tokens + int(usage.get("input_tokens") or 0),
            output_tokens=self.output_tokens + int(usage.get("output_tokens") or 0),
            cached_tokens=self.cached_tokens + int(usage.get("cached_tokens") or 0),
            cache_write_tokens=self.cache_write_tokens + int(usage.get("cache_write_tokens") or 0),
            cost_usd=self.cost_usd + float(usage.get("cost_usd") or 0.0),
            model_calls=self.model_calls + 1,
            model_latency_ms=self.model_latency_ms + float(latency_ms or 0.0),
        )

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class State:
    session_id: str = ""
    task: str = ""
    config: AgentConfig = field(default_factory=AgentConfig)
    workspace: str = ""
    parent: Mapping[str, Any] | None = None
    depth: int = 0
    status: str = "new"  # new | running | awaiting_approval | sleeping | finished
    next_seq: int = 0
    step: int = 0  # model responses so far
    plan_only_steps: int = 0  # model responses whose only tool calls were todo_write
    pending: tuple[ToolCall, ...] = ()
    verdicts: Mapping[str, Mapping[str, Any]] = _EMPTY
    approvals: Mapping[str, bool] = _EMPTY
    awaiting: str | None = None
    awaiting_expires_at: float | None = None  # when the pending approval lapses (approval_ttl_s)
    expired_approvals: frozenset[str] = frozenset()
    in_flight: str | None = None
    taint: frozenset[str] = frozenset()
    usage: Usage = field(default_factory=Usage)
    tool_calls: int = 0
    tool_errors: int = 0
    denied_calls: int = 0
    model_errors: int = 0
    consecutive_model_errors: int = 0
    loop_warnings: int = 0
    recent_signatures: tuple[str, ...] = ()
    todos: tuple[Mapping[str, Any], ...] = ()
    connectors_loaded: bool = False
    connector_tools: tuple[Mapping[str, Any], ...] = ()      # recorded tool entries (connectors.tool_entry)
    connector_servers: Mapping[str, Mapping[str, Any]] = _EMPTY
    wake_at: float | None = None
    folded_through: int = 0
    compactions: int = 0
    last_answer: str | None = None
    final: Mapping[str, Any] | None = None

    def pending_call(self, call_id: str) -> ToolCall | None:
        for call in self.pending:
            if call.id == call_id:
                return call
        return None


def _with(mapping: Mapping[str, Any], key: str, value: Any) -> Mapping[str, Any]:
    updated = dict(mapping)
    updated[key] = value
    return MappingProxyType(updated)


def reduce(state: State, event: Event) -> State:
    """Advance `state` by one event. Pure: same inputs, same output."""
    if event.seq != state.next_seq:
        raise ValueError(f"event seq {event.seq} applied to state expecting {state.next_seq}")
    d = event.data
    s = dataclasses.replace(state, next_seq=state.next_seq + 1)
    t = event.type

    if t == ev.SESSION_STARTED:
        return dataclasses.replace(
            s,
            session_id=event.session_id,
            task=d.get("task", ""),
            config=AgentConfig.from_dict(d.get("config")),
            workspace=d.get("workspace", ""),
            parent=MappingProxyType(dict(d["parent"])) if d.get("parent") else None,
            depth=int(d.get("depth", 0)),
            status="running",
        )

    if t == ev.USER_MESSAGE:
        # A follow-up message re-opens a finished session.
        return dataclasses.replace(s, status="running", final=None, last_answer=None)

    if t == ev.MODEL_RESPONSE:
        msg = d.get("message", {})
        calls = tuple(ToolCall.from_dict(c) for c in msg.get("tool_calls") or ())
        plan_only = bool(calls) and all(c.name == "todo_write" for c in calls)
        return dataclasses.replace(
            s,
            step=s.step + 1,
            plan_only_steps=s.plan_only_steps + (1 if plan_only else 0),
            usage=s.usage.add(d.get("usage") or {}, d.get("latency_ms", 0.0)),
            consecutive_model_errors=0,
            pending=calls,
            last_answer=None if calls else (msg.get("content") or ""),
        )

    if t == ev.MODEL_ERROR:
        return dataclasses.replace(
            s, model_errors=s.model_errors + 1, consecutive_model_errors=s.consecutive_model_errors + 1
        )

    if t == ev.POLICY_DECISION:
        return dataclasses.replace(s, verdicts=_with(s.verdicts, d["call_id"], dict(d)))

    if t == ev.APPROVAL_REQUESTED:
        return dataclasses.replace(s, awaiting=d["call_id"], awaiting_expires_at=d.get("expires_at"),
                                   status="awaiting_approval")

    if t == ev.APPROVAL_DECIDED:
        expired = s.expired_approvals | {d["call_id"]} if d.get("by") == "expired" else s.expired_approvals
        clears = s.awaiting == d["call_id"]
        return dataclasses.replace(
            s,
            approvals=_with(s.approvals, d["call_id"], bool(d.get("approved"))),
            awaiting=None if clears else s.awaiting,
            awaiting_expires_at=None if clears else s.awaiting_expires_at,
            expired_approvals=expired,
            status="running",
        )

    if t == ev.TOOL_STARTED:
        window = max(state.config.loop_window, 1)
        sigs = (s.recent_signatures + (d.get("signature", ""),))[-window:]
        return dataclasses.replace(s, in_flight=d["call_id"], recent_signatures=sigs)

    if t == ev.TOOL_COMPLETED:
        call_id = d["call_id"]
        kind = d.get("kind", "executed")
        taint = s.taint
        if kind in EXECUTED_KINDS:
            verdict = s.verdicts.get(call_id) or {}
            taint = taint | frozenset(verdict.get("properties") or ())
            # A sub-agent's taint flows back with its answer.
            taint = taint | frozenset(d.get("taint") or ())
        todos = s.todos
        if d.get("todos") is not None:
            todos = tuple(MappingProxyType(dict(item)) for item in d["todos"])
        wake_at = d.get("wake_at")
        return dataclasses.replace(
            s,
            in_flight=None if s.in_flight == call_id else s.in_flight,
            pending=tuple(c for c in s.pending if c.id != call_id),
            tool_calls=s.tool_calls + 1,
            tool_errors=s.tool_errors + (0 if d.get("ok") else 1),
            denied_calls=s.denied_calls + (1 if kind in ("denied", "rejected") else 0),
            loop_warnings=s.loop_warnings + (1 if d.get("loop_warning") else 0),
            taint=taint,
            todos=todos,
            wake_at=wake_at if wake_at is not None else s.wake_at,
            status="sleeping" if wake_at is not None else s.status,
        )

    if t == ev.CONTEXT_FOLDED:
        return dataclasses.replace(s, folded_through=max(s.folded_through, int(d["through_step"])))

    if t == ev.CONTEXT_COMPACTED:
        usage = s.usage.add(d["usage"], d.get("latency_ms", 0.0)) if d.get("usage") else s.usage
        return dataclasses.replace(s, compactions=s.compactions + 1, usage=usage)

    if t == ev.SESSION_FINISHED:
        return dataclasses.replace(s, status="finished", final=MappingProxyType(dict(d)))

    if t == ev.NOTE:
        if d.get("kind") == "woke":
            return dataclasses.replace(s, wake_at=None, status="running")
        if d.get("kind") == "connector_tools":
            return dataclasses.replace(
                s, connectors_loaded=True,
                connector_tools=tuple(MappingProxyType(dict(t)) for t in d.get("tools") or ()),
                connector_servers=MappingProxyType({k: MappingProxyType(dict(v)) for k, v in (d.get("servers") or {}).items()}),
            )
        return s

    raise ValueError(f"unknown event type {t!r}")


def replay(events: Iterable[Event], initial: State | None = None) -> State:
    state = initial or State()
    for event in events:
        state = reduce(state, event)
    return state
