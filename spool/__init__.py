"""Spool: an event-sourced runtime for long-running agents.

Every step an agent takes is appended to a per-session event log before it has
any side effect. State, model context, crash recovery, replay, forking and eval
metrics are all derived from that log.
"""

__version__ = "0.1.0"
