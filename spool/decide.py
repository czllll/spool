"""The agent loop's decision function.

`decide(state, now)` looks at the folded state and names the next action. It
is pure; the runtime performs the action and appends the resulting events.
Crash recovery needs no special path: after replaying the log, `decide` sees
an in-flight tool call (TOOL_STARTED without TOOL_COMPLETED) and asks for it
to be finished.
"""

from __future__ import annotations

from dataclasses import dataclass

from .state import State, ToolCall


@dataclass(frozen=True)
class Action:
    pass


@dataclass(frozen=True)
class CallModel(Action):
    pass


@dataclass(frozen=True)
class Authorize(Action):
    """Run the policy engine on a call and record its verdict."""

    call: ToolCall


@dataclass(frozen=True)
class RequestApproval(Action):
    call: ToolCall
    reason: str


@dataclass(frozen=True)
class ExecuteTool(Action):
    call: ToolCall
    recovering: bool = False  # True when TOOL_STARTED is already in the log


@dataclass(frozen=True)
class CompleteWithoutRunning(Action):
    """Close a tool call that must not run: denied, rejected or malformed."""

    call: ToolCall
    kind: str  # denied | rejected | invalid
    reason: str


@dataclass(frozen=True)
class Finish(Action):
    status: str  # completed | failed | max_steps | budget_exhausted
    answer: str = ""
    reason: str = ""


@dataclass(frozen=True)
class WaitForApproval(Action):
    call_id: str


@dataclass(frozen=True)
class Sleep(Action):
    until: float


@dataclass(frozen=True)
class Wake(Action):
    pass


@dataclass(frozen=True)
class Done(Action):
    pass


def decide(state: State, now: float) -> Action:
    cfg = state.config
    if state.status == "finished":
        return Done()
    if state.awaiting is not None:
        return WaitForApproval(state.awaiting)
    if state.wake_at is not None:
        return Sleep(state.wake_at) if now < state.wake_at else Wake()

    if state.in_flight is not None:
        call = state.pending_call(state.in_flight)
        if call is not None:
            return ExecuteTool(call, recovering=True)

    if state.pending:
        call = state.pending[0]
        if call.parse_error:
            return CompleteWithoutRunning(call, "invalid", f"Invalid tool call arguments: {call.parse_error}")
        verdict = state.verdicts.get(call.id)
        if verdict is None:
            return Authorize(call)
        if verdict["verdict"] == "deny":
            return CompleteWithoutRunning(call, "denied", verdict.get("reason", "denied by policy"))
        if verdict["verdict"] == "ask":
            if call.id not in state.approvals:
                return RequestApproval(call, verdict.get("reason", ""))
            if not state.approvals[call.id]:
                return CompleteWithoutRunning(call, "rejected", "The approver rejected this action.")
        return ExecuteTool(call)

    if state.last_answer is not None:
        return Finish("completed", answer=state.last_answer)
    if state.consecutive_model_errors >= cfg.max_model_errors:
        return Finish("failed", reason=f"{state.consecutive_model_errors} consecutive model errors")
    # Up to `free_plan_steps` turns spent only on updating the plan are not charged
    # against the step budget (see docs/notes/failures.md, 0002).
    charged = state.step - min(state.plan_only_steps, cfg.free_plan_steps)
    if charged >= cfg.max_steps:
        return Finish("max_steps", reason=f"reached max_steps={cfg.max_steps}")
    if state.usage.total_tokens >= cfg.max_total_tokens:
        return Finish("budget_exhausted", reason=f"used {state.usage.total_tokens} tokens")
    return CallModel()
