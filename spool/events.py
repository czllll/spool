"""Event vocabulary of a session.

A session is nothing but an ordered list of these events. The reducer
(`spool.state.reduce`) folds them into control-flow state, the context engine
renders them into model requests, and the eval harness reads metrics off them.
Events are never updated or deleted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

SESSION_STARTED = "session_started"      # task, config, workspace, parent pointer
USER_MESSAGE = "user_message"            # text from the user (the task, or a follow-up)
MODEL_RESPONSE = "model_response"        # one model turn: text, tool calls, usage, request digests
MODEL_ERROR = "model_error"              # a model call failed after retries
POLICY_DECISION = "policy_decision"      # allow / deny / ask for one tool call, with the rule and properties
APPROVAL_REQUESTED = "approval_requested"
APPROVAL_DECIDED = "approval_decided"
TOOL_STARTED = "tool_started"            # write-ahead intent record, appended before the tool runs
TOOL_COMPLETED = "tool_completed"        # result of a tool call (or of a denial / invalid call)
CONTEXT_FOLDED = "context_folded"        # older tool outputs render as stubs from here on (reversible)
CONTEXT_COMPACTED = "context_compacted"  # older turns render as a model-written summary (lossy baseline)
SESSION_FINISHED = "session_finished"    # terminal: completed / failed / max_steps / budget_exhausted / cancelled
NOTE = "note"                            # bookkeeping: wake-ups, crash recovery, warnings

ALL_TYPES = frozenset(
    {
        SESSION_STARTED,
        USER_MESSAGE,
        MODEL_RESPONSE,
        MODEL_ERROR,
        POLICY_DECISION,
        APPROVAL_REQUESTED,
        APPROVAL_DECIDED,
        TOOL_STARTED,
        TOOL_COMPLETED,
        CONTEXT_FOLDED,
        CONTEXT_COMPACTED,
        SESSION_FINISHED,
        NOTE,
    }
)


@dataclass(frozen=True)
class Event:
    session_id: str
    seq: int
    type: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = 0.0

    def get(self, key: str, default: Any = None) -> Any:
        return self.data.get(key, default)
