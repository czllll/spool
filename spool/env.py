"""A Gym-style environment over Spool tasks, for RL rollouts.

    env = SpoolEnv(task, data_dir="rollouts")
    obs = await env.reset()                         # the request the harness would send to a model
    obs, reward, done, info = await env.step(action)  # action = one assistant turn

The environment *is* the harness: tool calls go through the same policy
engine, sandbox and event log as a normal run, and the reward is the task's
outcome verifier (1.0 pass, 0.0 otherwise). Every rollout is a replayable
session in the store.
"""

from __future__ import annotations

import shutil
import uuid
from pathlib import Path
from typing import Any

from .config import AgentConfig
from .eval.runner import _verify
from .eval.task import Task
from .models.base import ModelRequest, ModelResponse
from .runtime import Runtime
from .store import EventStore
from .util import canonical_json


class NeedAction(Exception):
    """Raised inside the runtime when the harness needs the next assistant turn."""

    def __init__(self, request: ModelRequest) -> None:
        super().__init__("waiting for an action")
        self.request = request


class _ExternalModel:
    name = "external"

    def __init__(self) -> None:
        self.pending: ModelResponse | None = None

    async def complete(self, request: ModelRequest) -> ModelResponse:
        if self.pending is None:
            raise NeedAction(request)
        response, self.pending = self.pending, None
        return response

    async def aclose(self) -> None:
        return None


def _observation(request: ModelRequest) -> dict[str, Any]:
    return {"system": request.system, "messages": request.messages, "tools": request.tools}


class SpoolEnv:
    def __init__(self, task: Task, *, config: AgentConfig | None = None, data_dir: str | Path = ".spool/rollouts") -> None:
        self.task = task
        self.config = config or AgentConfig(sandbox="auto")
        self.data_dir = Path(data_dir)
        self.model = _ExternalModel()
        self.store = EventStore(self.data_dir / "spool.db")
        self.runtime: Runtime | None = None
        self.session_id: str | None = None
        self.workspace: Path | None = None

    async def reset(self) -> dict[str, Any]:
        if self.runtime is not None:
            await self.runtime.aclose()
        self.workspace = self.data_dir / "workspaces" / uuid.uuid4().hex[:10]
        if self.workspace.exists():
            shutil.rmtree(self.workspace)
        self.task.prepare_workspace(self.workspace)
        overrides: dict[str, Any] = dict(self.task.overrides)
        if self.task.max_steps:
            overrides["max_steps"] = self.task.max_steps
        if self.task.tools is not None:
            overrides["tools"] = self.task.tools
        self.runtime = Runtime(self.store, self.model, data_dir=self.data_dir, crash_hook=None)
        self.session_id = self.runtime.start(self.task.prompt, config=self.config.replace(**overrides),
                                             workspace=self.workspace, fixtures=self.task.fixtures())
        obs, _, done, _ = await self._advance()
        assert not done
        return obs

    async def step(self, action: dict[str, Any]) -> tuple[dict[str, Any] | None, float, bool, dict[str, Any]]:
        calls = []
        for i, c in enumerate(action.get("tool_calls") or []):
            args = c.get("arguments") or {}
            calls.append({"id": c.get("id") or f"a{i}_{uuid.uuid4().hex[:6]}", "name": c["name"], "arguments": args,
                          "raw_arguments": canonical_json(args), "parse_error": None})
        self.model.pending = ModelResponse(
            content=action.get("content", ""), tool_calls=calls, stop_reason="tool_use" if calls else "end_turn",
            usage={"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_write_tokens": 0, "cost_usd": 0.0},
            model="external", latency_ms=0.0,
        )
        return await self._advance()

    async def _advance(self) -> tuple[dict[str, Any] | None, float, bool, dict[str, Any]]:
        assert self.runtime is not None and self.session_id is not None
        try:
            result = await self.runtime.run(self.session_id)
        except NeedAction as need:
            return _observation(need.request), 0.0, False, {"session_id": self.session_id}
        passed, detail = await _verify(self.task, self.workspace, self.runtime.ledger_dir(self.session_id))
        reward = 1.0 if passed and result.final_status == "completed" else 0.0
        info = {"session_id": self.session_id, "status": result.final_status or result.status, "verifier": detail}
        return None, reward, True, info

    async def close(self) -> None:
        if self.runtime is not None:
            await self.runtime.aclose()
        self.store.close()
