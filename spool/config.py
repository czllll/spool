"""Agent configuration.

A session's config is frozen into its SESSION_STARTED event, so a session
renders, decides and resumes the same way later, even if defaults change.
Eval ablations are just different configs (see evals/configs/*.toml).
"""

from __future__ import annotations

import dataclasses
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CONTEXT_STRATEGIES = ("full", "offload", "fold", "summarize")
POLICIES = ("off", "rule_of_two")
APPROVAL_MODES = ("auto_approve", "auto_deny", "human")
SANDBOXES = ("auto", "process", "seatbelt", "docker")


@dataclass(frozen=True)
class AgentConfig:
    # model
    model: str = "local"
    max_output_tokens: int = 2048
    temperature: float | None = 0.6
    # loop limits
    max_steps: int = 30
    free_plan_steps: int = 5               # turns that only call todo_write and are not charged to max_steps
    max_model_errors: int = 3
    max_total_tokens: int = 2_000_000
    # context engine
    context_strategy: str = "offload"      # full | offload | fold | summarize
    context_budget_tokens: int = 24_000    # what the engine aims to stay under
    compact_trigger: float = 0.75          # fraction of the budget that triggers fold / summarize
    keep_recent_steps: int = 4             # steps that are never folded or summarized
    offload_threshold_chars: int = 3_000   # tool outputs above this may be written to a file
    offload_when_over: float = 0.5         # ...but only once the prompt would pass this share of the budget
                                           # (0 = always; outputs over hard_cap_chars are always offloaded)
    preview_chars: int = 1_200             # how much of an offloaded output stays inline
    hard_cap_chars: int = 40_000           # absolute cap for any inline tool output
    recite_todos: bool = True              # re-state the plan at the tail every few tool calls
    recite_every: int = 4
    unstable_prefix: bool = False          # ablation: put the wall clock at the top of the system prompt
    system_prompt: str | None = None
    # policy
    policy: str = "rule_of_two"            # off | rule_of_two
    approval: str = "auto_deny"            # auto_approve | auto_deny | human
    approval_ttl_s: float | None = None    # a pending approval expires after this long and the call does not run
                                           # (None = wait forever; routine runs default to 600, see routines.py)
    sensitive_paths: tuple[str, ...] = ("secrets",)
    untrusted_paths: tuple[str, ...] = ("downloads", "inbox")
    # tools and sandbox
    sandbox: str = "auto"                  # auto | process | seatbelt | docker
    tool_timeout_s: float = 120.0
    tools: tuple[str, ...] | None = None   # allow-list of tool names; None = all built-ins
    connectors: tuple[str, ...] = ()       # MCP connectors whose tools this session gets (see connectors.py)
    skills: tuple[str, ...] = ()           # Agent Skills the session may load ("*" = all installed; see skills.py)
    memory: bool = False                   # cross-session memory: a snapshot in the prompt, remember/search_memory
    memory_budget_chars: int = 2_000       # how much memory the snapshot may show (most relevant first)
    loop_window: int = 6
    loop_threshold: int = 3
    max_subagent_steps: int = 12
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.context_strategy not in CONTEXT_STRATEGIES:
            raise ValueError(f"context_strategy must be one of {CONTEXT_STRATEGIES}")
        if self.policy not in POLICIES:
            raise ValueError(f"policy must be one of {POLICIES}")
        if self.approval not in APPROVAL_MODES:
            raise ValueError(f"approval must be one of {APPROVAL_MODES}")
        if self.sandbox not in SANDBOXES:
            raise ValueError(f"sandbox must be one of {SANDBOXES}")
        if self.approval_ttl_s is not None and self.approval_ttl_s <= 0:
            raise ValueError("approval_ttl_s must be positive (or None to wait forever)")

    def to_dict(self) -> dict[str, Any]:
        out = dataclasses.asdict(self)
        for key, value in out.items():
            if isinstance(value, tuple):
                out[key] = list(value)
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> "AgentConfig":
        data = dict(data or {})
        names = {f.name for f in dataclasses.fields(cls)}
        unknown = set(data) - names
        if unknown:
            raise ValueError(f"unknown config keys: {sorted(unknown)}")
        for f in dataclasses.fields(cls):
            if f.name in data and isinstance(data[f.name], list):
                data[f.name] = tuple(data[f.name])
        return cls(**data)

    @classmethod
    def from_toml(cls, path: str | Path) -> "AgentConfig":
        with open(path, "rb") as fh:
            raw = tomllib.load(fh)
        raw.pop("name", None)
        raw.pop("description", None)
        return cls.from_dict(raw)

    def replace(self, **changes: Any) -> "AgentConfig":
        return dataclasses.replace(self, **changes)
