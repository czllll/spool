"""The runtime: the imperative shell around the pure loop.

    events --replay--> state --decide--> action --perform--> new events

The one rule: an event is appended *before* the effect it describes becomes
visible to anyone else. TOOL_STARTED (with an idempotency key) is written
before a tool runs; the result is written as TOOL_COMPLETED. If the process
dies in between, the next `run` replays the log, sees the open call, and
`decide` asks for it to be finished:
  - idempotent tools run again;
  - ledgered tools run again with the same key, and the ledger returns the
    first result instead of repeating the side effect;
  - anything else (bash) is reported to the model as "interrupted, effects
    unknown" rather than silently run twice.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from . import events as ev
from . import policy
from .config import AgentConfig
from .context import CHARS_PER_TOKEN, estimate_tokens, plan_compaction, plan_fold, render, summary_request
from .decide import (
    Authorize,
    CallModel,
    CompleteWithoutRunning,
    Done,
    ExecuteTool,
    Finish,
    RequestApproval,
    Sleep,
    Wake,
    WaitForApproval,
    decide,
)
from .events import Event
from .models.base import ModelClient, ModelError
from .sandbox import Sandbox, make_sandbox
from .state import State, ToolCall, reduce, replay
from .store import EventStore
from .tools import INTERNAL_DIR, ToolContext, ToolRegistry, ToolResult, validate_args
from .tools.builtin import DEFAULT_TOOLS, builtin_registry, render_todos
from .util import digest, new_id, truncate_middle


class SimulatedCrash(BaseException):
    """Raised by test crash hooks to stop the runtime as abruptly as kill -9 would.

    BaseException so that no `except Exception` in the runtime can swallow it.
    """


@dataclass
class RunResult:
    session_id: str
    status: str  # finished | awaiting_approval | sleeping | paused
    state: State

    @property
    def final_status(self) -> str | None:
        return self.state.final.get("status") if self.state.final else None

    @property
    def answer(self) -> str:
        return (self.state.final or {}).get("answer", "") if self.state.final else ""


class _Handle:
    """An open session: its events, its folded state, and its sandbox."""

    def __init__(self, store: EventStore, events: list[Event], on_event: Callable[[Event], None] | None) -> None:
        self.store = store
        self.events = events
        self.state = replay(events)
        self.on_event = on_event
        self.sandbox: Sandbox | None = None
        self._completed: dict[str, dict[str, Any]] = {
            e.data["call_id"]: e.data for e in events if e.type == ev.TOOL_COMPLETED
        }

    @property
    def session_id(self) -> str:
        return self.state.session_id

    def append(self, type: str, data: dict[str, Any]) -> Event:
        event = self.store.append(self.session_id, type, data, expected_seq=self.state.next_seq)
        self.events.append(event)
        self.state = reduce(self.state, event)
        if type == ev.TOOL_COMPLETED:
            self._completed[event.data["call_id"]] = event.data
        if self.on_event is not None:
            self.on_event(event)
        return event

    def completed(self, call_id: str) -> Mapping[str, Any] | None:
        return self._completed.get(call_id)


class Runtime:
    def __init__(
        self,
        store: EventStore,
        model: ModelClient,
        *,
        tools: ToolRegistry | None = None,
        data_dir: str | Path = ".spool",
        sandbox_factory: Callable[[Path, AgentConfig], Sandbox] | None = None,
        summarizer: ModelClient | None = None,
        clock: Callable[[], float] = time.time,
        crash_hook: Callable[[str, Mapping[str, Any]], None] | None = None,
        on_event: Callable[[Event], None] | None = None,
    ) -> None:
        self.store = store
        self.model = model
        self.tools = tools or builtin_registry()
        self.data_dir = Path(data_dir)
        self.sandbox_factory = sandbox_factory or (
            lambda ws, cfg: make_sandbox(cfg.sandbox, ws, protected=cfg.sensitive_paths)
        )
        self.summarizer = summarizer
        self.clock = clock
        self.crash_hook = crash_hook if crash_hook is not None else crash_hook_from_env()
        self.on_event = on_event
        self._handles: dict[str, _Handle] = {}

    # ---------------------------------------------------------------- sessions
    def start(
        self,
        task: str,
        *,
        config: AgentConfig | None = None,
        workspace: str | Path | None = None,
        session_id: str | None = None,
        fixtures: Mapping[str, Any] | None = None,
        parent: Mapping[str, Any] | None = None,
        depth: int = 0,
    ) -> str:
        config = config or AgentConfig()
        sid = session_id or new_id("s")
        ws = Path(workspace) if workspace else self.data_dir / "workspaces" / sid
        ws.mkdir(parents=True, exist_ok=True)
        ws = Path(os.path.realpath(ws))
        h = _Handle(self.store, [], self.on_event)
        h.state = dataclasses.replace(h.state, session_id=sid)
        h.append(ev.SESSION_STARTED, {
            "task": task,
            "config": config.to_dict(),
            "workspace": str(ws),
            "parent": dict(parent) if parent else None,
            "depth": depth,
            "fixtures": dict(fixtures or {}),
        })
        h.append(ev.USER_MESSAGE, {"text": task})
        self._handles[sid] = h
        return sid

    def send(self, session_id: str, text: str) -> None:
        self._open(session_id).append(ev.USER_MESSAGE, {"text": text})

    def approve(self, session_id: str, call_id: str, approved: bool, *, by: str = "user", note: str = "") -> None:
        h = self._open(session_id)
        if h.state.awaiting != call_id:
            raise ValueError(f"session {session_id} is not waiting for approval of {call_id!r} (waiting for {h.state.awaiting!r})")
        h.append(ev.APPROVAL_DECIDED, {"call_id": call_id, "approved": approved, "by": by, "note": note})

    def fork(self, session_id: str, at_seq: int, *, config: AgentConfig | None = None,
             copy_workspace: bool = True) -> str:
        """New session whose history is `session_id`'s events 0..at_seq.

        The workspace is copied as it is now, not as it was at `at_seq`; forks
        are exact for the log and the context, approximate for files.
        """
        source = self.store.load(session_id, upto_seq=at_seq)
        if not source or source[0].type != ev.SESSION_STARTED:
            raise ValueError(f"no session {session_id}")
        new_sid = new_id("s")
        started = dict(source[0].data)
        if config is not None:
            started["config"] = config.to_dict()
        old_ws = Path(started["workspace"])
        new_ws = self.data_dir / "workspaces" / new_sid
        if copy_workspace and old_ws.exists():
            shutil.copytree(old_ws, new_ws, ignore=shutil.ignore_patterns(".spool"))
        new_ws.mkdir(parents=True, exist_ok=True)
        started["workspace"] = str(Path(os.path.realpath(new_ws)))
        started["parent"] = {"session_id": session_id, "seq": at_seq, "kind": "fork"}
        h = _Handle(self.store, [], self.on_event)
        h.state = dataclasses.replace(h.state, session_id=new_sid)
        h.append(ev.SESSION_STARTED, started)
        for event in source[1:]:
            h.append(event.type, event.data)
        self._handles[new_sid] = h
        return new_sid

    def _open(self, session_id: str) -> _Handle:
        h = self._handles.get(session_id)
        if h is None:
            events = self.store.load(session_id)
            if not events:
                raise KeyError(f"no session {session_id}")
            h = _Handle(self.store, events, self.on_event)
            self._handles[session_id] = h
        return h

    def state(self, session_id: str) -> State:
        return self._open(session_id).state

    async def aclose(self) -> None:
        for h in self._handles.values():
            if h.sandbox is not None:
                await h.sandbox.close()
                h.sandbox = None
        self._handles.clear()

    # --------------------------------------------------------------------- run
    async def run(self, session_id: str, *, max_actions: int | None = None) -> RunResult:
        h = self._open(session_id)
        performed = 0
        while True:
            action = decide(h.state, now=self.clock())
            if isinstance(action, Done):
                return RunResult(session_id, "finished", h.state)
            if isinstance(action, WaitForApproval):
                return RunResult(session_id, "awaiting_approval", h.state)
            if isinstance(action, Sleep):
                return RunResult(session_id, "sleeping", h.state)
            if max_actions is not None and performed >= max_actions:
                return RunResult(session_id, "paused", h.state)
            performed += 1

            if isinstance(action, CallModel):
                await self._call_model(h)
            elif isinstance(action, Authorize):
                self._authorize(h, action.call)
            elif isinstance(action, RequestApproval):
                h.append(ev.APPROVAL_REQUESTED, {
                    "call_id": action.call.id, "tool": action.call.name,
                    "arguments": dict(action.call.args), "reason": action.reason,
                })
            elif isinstance(action, CompleteWithoutRunning):
                h.append(ev.TOOL_COMPLETED, {
                    "call_id": action.call.id, "name": action.call.name, "ok": False,
                    "kind": action.kind, "error": action.reason, "output": "",
                })
            elif isinstance(action, ExecuteTool):
                await self._execute(h, action.call, action.recovering)
            elif isinstance(action, Wake):
                h.append(ev.NOTE, {"kind": "woke", "at": self.clock()})
            elif isinstance(action, Finish):
                h.append(ev.SESSION_FINISHED, {
                    "status": action.status, "answer": action.answer, "reason": action.reason,
                    "steps": h.state.step, "usage": dataclasses.asdict(h.state.usage),
                })
            else:  # pragma: no cover - decide() returns a closed set
                raise RuntimeError(f"unhandled action {action!r}")

    # ------------------------------------------------------------- model step
    def _registry(self, cfg: AgentConfig) -> ToolRegistry:
        return self.tools.subset(cfg.tools if cfg.tools is not None else DEFAULT_TOOLS)

    async def _call_model(self, h: _Handle) -> None:
        cfg = h.state.config
        schemas = self._registry(cfg).schemas()
        rendered_at = self.clock()
        request = render(h.events, h.state, schemas, now=rendered_at)
        estimated = estimate_tokens(request)

        fold_to = plan_fold(h.state, estimated)
        if fold_to is not None:
            h.append(ev.CONTEXT_FOLDED, {"through_step": fold_to, "estimated_tokens_before": estimated})
            request = render(h.events, h.state, schemas, now=rendered_at)

        upto = plan_compaction(h.events, h.state, estimated)
        if upto is not None:
            try:
                summary = await (self.summarizer or self.model).complete(summary_request(h.events, h.state, upto))
            except ModelError as exc:
                h.append(ev.MODEL_ERROR, {"error": f"compaction failed: {exc}", "retriable": exc.retriable})
                return
            h.append(ev.CONTEXT_COMPACTED, {
                "upto_seq": upto, "summary": summary.content, "usage": summary.usage,
                "latency_ms": round(summary.latency_ms, 1), "estimated_tokens_before": estimated,
            })
            request = render(h.events, h.state, schemas, now=rendered_at)

        self._crash("before_model_call", {"step": h.state.step + 1})
        try:
            response = await self.model.complete(request)
        except ModelError as exc:
            h.append(ev.MODEL_ERROR, {"error": str(exc), "retriable": exc.retriable, "status": exc.status})
            return
        data = response.to_event_data()
        data["request"] = request.digest_parts()
        data["rendered_at"] = rendered_at
        data["estimated_tokens"] = estimate_tokens(request)
        _sanitize_calls(data["message"]["tool_calls"], response.stop_reason)
        h.append(ev.MODEL_RESPONSE, data)
        self._crash("after_model_response", {"step": h.state.step})

    # -------------------------------------------------------------- tool step
    def _authorize(self, h: _Handle, call: ToolCall) -> None:
        registry = self._registry(h.state.config)
        spec = registry.get(call.name)
        if spec is None:
            h.append(ev.TOOL_COMPLETED, {
                "call_id": call.id, "name": call.name, "ok": False, "kind": "invalid", "output": "",
                "error": f"Unknown tool {call.name!r}. Available tools: {', '.join(registry.names())}.",
            })
            return
        problems = validate_args(spec.parameters, dict(call.args))
        if problems:
            h.append(ev.TOOL_COMPLETED, {
                "call_id": call.id, "name": call.name, "ok": False, "kind": "invalid", "output": "",
                "error": f"Invalid arguments for {call.name}: " + "; ".join(problems),
            })
            return
        isolation = self._sandbox(h).isolation
        h.append(ev.POLICY_DECISION, policy.evaluate(call, spec, h.state, isolation=isolation))

    def _sandbox(self, h: _Handle) -> Sandbox:
        if h.sandbox is None:
            h.sandbox = self.sandbox_factory(Path(h.state.workspace), h.state.config)
        return h.sandbox

    async def _execute(self, h: _Handle, call: ToolCall, recovering: bool) -> None:
        cfg = h.state.config
        spec = self._registry(cfg).get(call.name)
        if spec is None:  # config changed under a recovering session
            h.append(ev.TOOL_COMPLETED, {"call_id": call.id, "name": call.name, "ok": False,
                                         "kind": "invalid", "error": "tool no longer available", "output": ""})
            return
        sid = h.state.session_id
        idem_key = f"{sid}:{call.id}"
        signature = digest({"name": call.name, "args": dict(call.args)}, 16)
        if not recovering:
            h.append(ev.TOOL_STARTED, {
                "call_id": call.id, "name": call.name, "arguments": dict(call.args),
                "idem_key": idem_key, "signature": signature,
            })
            self._crash("after_tool_started", {"tool": call.name, "call_id": call.id})
        elif not (spec.idempotent or spec.ledgered):
            h.append(ev.TOOL_COMPLETED, {
                "call_id": call.id, "name": call.name, "ok": False, "kind": "interrupted", "output": "",
                "error": ("The process stopped while this command was running, before its result was recorded. "
                          "It may or may not have taken effect. Check the workspace before running it again."),
            })
            return
        else:
            h.append(ev.NOTE, {"kind": "recovery", "call_id": call.id, "tool": call.name})

        ctx = ToolContext(
            workspace=Path(h.state.workspace),
            sandbox=self._sandbox(h),
            session_id=sid,
            call_id=call.id,
            idem_key=idem_key,
            config=cfg,
            ledger_dir=self.ledger_dir(sid if h.state.depth == 0 else (h.state.parent or {}).get("session_id", sid)),
            fixtures=self._fixtures(h),
            history=h.completed,
            now=self.clock,
            spawn=(lambda task, steps: self._spawn(h, call, task, steps)) if h.state.depth == 0 else None,
        )
        started = time.perf_counter()
        try:
            result = await asyncio.wait_for(spec.handler(ctx, dict(call.args)), timeout=cfg.tool_timeout_s + 30)
        except asyncio.TimeoutError:
            result = ToolResult.fail(f"tool {call.name} timed out")
        except Exception as exc:  # a tool bug must not kill the session
            result = ToolResult.fail(f"{type(exc).__name__}: {exc}")
        self._crash("after_tool_executed", {"tool": call.name, "call_id": call.id})

        data: dict[str, Any] = {
            "call_id": call.id,
            "name": call.name,
            "ok": result.ok,
            "kind": "executed",
            "output": result.output,
            "error": result.error,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }
        data.update(result.data)
        if (result.data.get("meta") or {}).get("deduplicated"):
            data["kind"] = "deduplicated"
        self._offload(h, data)
        repeats = h.state.recent_signatures.count(signature)
        if repeats >= cfg.loop_threshold:
            data["loop_warning"] = (
                f"[Warning: you have made this exact {call.name} call {repeats} times in your last "
                f"{len(h.state.recent_signatures)} calls. Repeating it will not help; change approach or finish.]"
            )
        if (cfg.recite_todos and h.state.todos and call.name != "todo_write"
                and (h.state.tool_calls + 1) % max(cfg.recite_every, 1) == 0):
            data["reminder"] = "[Your current plan]\n" + render_todos([dict(t) for t in h.state.todos])
        h.append(ev.TOOL_COMPLETED, data)

    def _offload(self, h: _Handle, data: dict[str, Any]) -> None:
        cfg = h.state.config
        output = data.get("output") or ""
        if cfg.context_strategy not in ("offload", "fold") or len(output) <= cfg.offload_threshold_chars:
            return
        if cfg.offload_when_over > 0 and len(output) <= cfg.hard_cap_chars:
            # Budget-aware: keep the output inline while the prompt has room. Offloading
            # early turns one tool call into two (call, then read the file), which cost
            # a step-limited small model whole tasks in the E2 evals (docs/notes/failures.md, 0004).
            if self._projected_tokens(h, output) < cfg.offload_when_over * cfg.context_budget_tokens:
                return
        rel = Path(INTERNAL_DIR) / "outputs" / f"{data['call_id']}.txt"
        path = Path(h.state.workspace) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(output, encoding="utf-8")
        data["offloaded_path"] = str(rel)
        data["full_chars"] = len(output)
        data["preview"] = truncate_middle(output, cfg.preview_chars)

    def _projected_tokens(self, h: _Handle, output: str) -> int:
        """Estimated size of the next prompt if `output` stays inline (a function of the log)."""
        base = 0
        pending_chars = len(output)
        for e in reversed(h.events):
            if e.type == ev.MODEL_RESPONSE:
                base = int(e.data.get("estimated_tokens") or 0)
                break
            if e.type == ev.TOOL_COMPLETED and not e.data.get("offloaded_path"):
                pending_chars += len(e.data.get("output") or "")
        return base + int(pending_chars / CHARS_PER_TOKEN)

    def _fixtures(self, h: _Handle) -> Mapping[str, Any]:
        return h.events[0].data.get("fixtures") or {} if h.events else {}

    def ledger_dir(self, session_id: str) -> Path:
        # Outside the workspace, so the agent's shell cannot tamper with it.
        return self.data_dir / "ledgers" / session_id

    async def _spawn(self, parent: _Handle, call: ToolCall, task: str, max_steps: int) -> dict[str, Any]:
        # Deterministic id: if the parent crashes mid-subtask, re-running the
        # spawn call resumes the same child instead of starting a second one.
        child_id = f"{parent.session_id}.{call.id}"
        if not self.store.exists(child_id):
            pcfg = parent.state.config
            allowed = tuple(t for t in (pcfg.tools or DEFAULT_TOOLS) if t != "spawn_subagent")
            cfg = pcfg.replace(max_steps=max_steps, tools=allowed)
            self.start(task, config=cfg, workspace=parent.state.workspace, session_id=child_id,
                       fixtures=self._fixtures(parent),
                       parent={"session_id": parent.session_id, "seq": parent.state.next_seq - 1,
                               "call_id": call.id, "kind": "subagent"},
                       depth=parent.state.depth + 1)
        result = await self.run(child_id)
        child = result.state
        final = child.final or {}
        return {
            "session_id": child_id,
            "status": final.get("status", result.status),
            "answer": final.get("answer") or final.get("reason") or "",
            "steps": child.step,
            "taint": sorted(child.taint),
            "usage": dataclasses.asdict(child.usage),
        }

    # ----------------------------------------------------------------- crashes
    def _crash(self, point: str, info: Mapping[str, Any]) -> None:
        if self.crash_hook is not None:
            self.crash_hook(point, info)


def _sanitize_calls(calls: list[dict[str, Any]], stop_reason: str) -> None:
    seen: set[str] = set()
    for i, c in enumerate(calls):
        if stop_reason == "max_tokens" and not c.get("parse_error"):
            c["parse_error"] = "the response was cut off at max_tokens, so these arguments may be incomplete; send a smaller call"
        if c["id"] in seen:  # some servers reuse ids within a response
            c["id"] = f"{c['id']}_{i}"
        seen.add(c["id"])
        if not c.get("name"):
            c["parse_error"] = c.get("parse_error") or "tool call has no name"


def crash_hook_from_env() -> Callable[[str, Mapping[str, Any]], None] | None:
    """SPOOL_CRASH_AT="point[:tool][:n]" kills this process with SIGKILL at that point.

    Used by the crash-injection evals to stop the agent at precise moments
    (e.g. after a side effect, before its result is logged) and then resume it
    in a fresh process.
    """
    spec = os.environ.get("SPOOL_CRASH_AT")
    if not spec:
        return None
    parts = spec.split(":")
    point = parts[0]
    tool = parts[1] if len(parts) > 1 and parts[1] else None
    nth = int(parts[2]) if len(parts) > 2 else 1
    seen = {"n": 0}

    def hook(at: str, info: Mapping[str, Any]) -> None:
        if at != point or (tool is not None and info.get("tool") != tool):
            return
        seen["n"] += 1
        if seen["n"] == nth:
            os.kill(os.getpid(), signal.SIGKILL)

    return hook
