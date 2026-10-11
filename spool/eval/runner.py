"""Run tasks x trials under one config and record outcome-graded results.

Results are appended to <out>/results.jsonl as each trial finishes, so a long
run can be interrupted and resumed (finished trials are skipped).

Crash mode: the session is driven by `spool resume` subprocesses. The first one
runs with SPOOL_CRASH_AT and is SIGKILLed at that point; later ones resume from
the log until the session finishes. This exercises real process death, not a
simulated exception.

Tasks with connectors get their own demo services per trial (seeded from the
task's services/ folder), a simulated user who answers approval requests as the
task says (approve, deny, or ignore them until they expire), and an audit of
the services' records against the log after the trial (spool.services.audit).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .. import events as ev
from ..config import AgentConfig
from ..connectors import ConnectorManager
from ..context import prefix_breaks
from ..models import make_model
from ..models.base import ModelClient, PinnedModel
from ..replay import verify_render
from ..models.scripted import ScriptedModel
from ..runtime import Runtime
from ..services.audit import audit
from ..state import State, replay
from ..store import EventStore
from ..tools.ledger import Ledger
from .metrics import summarize
from .task import Task


@dataclass
class EvalSpec:
    name: str
    config: AgentConfig
    model_spec: str = "local"
    trials: int = 3
    concurrency: int = 1
    crash_points: list[str] = field(default_factory=list)  # cycled over trials; empty = no crashes
    out_dir: Path = Path("evals/results/run")
    config_source: str | None = None


def _slug(text: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-")


def load_results(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


async def run_eval(
    tasks: list[Task],
    spec: EvalSpec,
    *,
    model: ModelClient | None = None,
    progress: Callable[[str], None] = print,
) -> list[dict[str, Any]]:
    spec.out_dir = out = Path(spec.out_dir).resolve()
    out.mkdir(parents=True, exist_ok=True)
    results_path = out / "results.jsonl"
    existing = load_results(results_path)
    done = {(r["task_id"], r["trial"]) for r in existing}
    (out / "spec.json").write_text(json.dumps({
        "name": spec.name, "model": spec.model_spec, "trials": spec.trials, "concurrency": spec.concurrency,
        "crash_points": spec.crash_points, "config": spec.config.to_dict(), "config_source": spec.config_source,
        "tasks": [t.id for t in tasks],
    }, indent=2))

    store = EventStore(out / "spool.db")
    model = model or make_model(spec.model_spec)
    # One server slot per concurrent trial, held for the whole trial (session affinity).
    slots: asyncio.Queue[int] = asyncio.Queue()
    for i in range(max(spec.concurrency, 1)):
        slots.put_nowait(i)
    lock = asyncio.Lock()
    jobs = [(t, i) for i in range(spec.trials) for t in tasks if (t.id, i) not in done]
    progress(f"[{spec.name}] {len(jobs)} trials to run ({len(done)} already done), concurrency {spec.concurrency}")
    new_results: list[dict[str, Any]] = []
    counter = {"n": 0}

    async def one(task: Task, trial: int) -> None:
        slot = await slots.get()
        try:
            crash = spec.crash_points[trial % len(spec.crash_points)] if spec.crash_points else None
            result = await run_trial(task, trial, spec, store, PinnedModel(model, slot), crash, slot=slot)
        finally:
            slots.put_nowait(slot)
        async with lock:
            with results_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            new_results.append(result)
            counter["n"] += 1
            mark = "PASS" if result["passed"] else "fail"
            progress(f"[{spec.name}] {counter['n']}/{len(jobs)} {task.id} t{trial} {mark} "
                     f"({result['status']}, {result['steps']} steps, {result['wall_ms'] / 1000:.0f}s)"
                     + (f" crash={crash}" if crash else ""))

    try:
        await asyncio.gather(*(one(t, i) for t, i in jobs))
    finally:
        if model is not None and hasattr(model, "aclose"):
            await model.aclose()
        store.close()
    all_results = existing + new_results
    (out / "summary.json").write_text(json.dumps(summarize(all_results), indent=2))
    return all_results


async def run_trial(task: Task, trial: int, spec: EvalSpec, store: EventStore, model: ModelClient,
                    crash: str | None, *, slot: int | None = None) -> dict[str, Any]:
    out = spec.out_dir
    ws = out / "workspaces" / f"{_slug(task.id)}-t{trial}"
    if ws.exists():
        # Only ever our own per-trial directory under the results dir.
        assert out.resolve() in ws.resolve().parents
        shutil.rmtree(ws)
    task.prepare_workspace(ws)
    overrides: dict[str, Any] = dict(task.overrides)
    if task.max_steps:
        overrides["max_steps"] = task.max_steps
    if task.tools is not None:
        overrides["tools"] = task.tools
    if task.connectors:
        overrides["connectors"] = task.connectors
    cfg = spec.config.replace(**overrides)

    connectors_file = task.connectors_file if task.connectors else None
    manager = ConnectorManager.from_file(connectors_file, data_dir=out)
    rt = Runtime(store, model, data_dir=out, crash_hook=None, connectors=manager)
    sid = rt.start(task.prompt, config=cfg, workspace=ws, fixtures=task.fixtures())
    ledger_dir = rt.ledger_dir(sid)
    if (task.dir / "services").is_dir():  # each trial gets fresh services, seeded from the task
        shutil.copytree(task.dir / "services", ledger_dir / "seeds", dirs_exist_ok=True)
    started = time.perf_counter()
    harness_error = None
    crashes = resumes = 0
    status = "unknown"
    try:
        if crash:
            crashes, resumes, status = await _drive_with_crashes(sid, crash, spec, store, task, slot, connectors_file)
        else:
            result = await asyncio.wait_for(_drive(rt, sid, task), timeout=task.timeout_s)
            status = result.final_status or result.status
    except asyncio.TimeoutError:
        status = "timeout"
    except Exception as exc:  # recorded, never raised: one broken trial must not end the run
        harness_error = f"{type(exc).__name__}: {exc}"
        status = "harness_error"
    finally:
        await rt.aclose()
        if manager is not None:
            await manager.aclose()
    wall_ms = (time.perf_counter() - started) * 1000

    passed, verify_out = await _verify(task, ws, ledger_dir)
    record = session_metrics(store, sid)
    if task.connectors:
        family = [e for row_id in [sid, *_children(store, sid)] for e in store.load(row_id)]
        record["audit"] = audit(ledger_dir, family)
    outbox = Ledger(ledger_dir / "outbox.jsonl").entries()
    net = Ledger(ledger_dir / "net.jsonl").entries()
    if record.get("status"):
        status = record["status"]
    return {
        "task_id": task.id,
        "trial": trial,
        "config": spec.name,
        "session_id": sid,
        # The outcome must check out *and* the agent must have finished on its own.
        "passed": passed and status == "completed",
        "verified": passed,
        "verify_output": verify_out,
        "status": record.get("status") or status,
        "harness_error": harness_error,
        "wall_ms": round(wall_ms),
        "crash_point": crash,
        "crashes": crashes,
        "resumes": resumes,
        "outbox_count": len(outbox),
        "net_requests": len(net),
        **{k: v for k, v in record.items() if k != "status"},
    }


async def _verify(task: Task, ws: Path, ledger_dir: Path) -> tuple[bool, str]:
    script = task.dir / "verify.py"
    if not script.exists():
        return False, "no verify.py"
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(script), str(ws), str(ledger_dir), cwd=ws,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        proc.kill()
        return False, "verifier timed out"
    text = out.decode("utf-8", "replace").strip()
    return proc.returncode == 0, text[:500]


APPROVER = "eval-approver"
MAX_APPROVALS = 20


async def _drive(rt: Runtime, sid: str, task: Task):
    """Run the session, answering approval requests the way the task's simulated user does."""
    result = await rt.run(sid)
    for _ in range(MAX_APPROVALS):
        if result.status != "awaiting_approval":
            break
        if not await _answer(rt, sid, result.state, task):
            break  # "ignore" without a deadline would wait forever: leave it waiting
        result = await rt.run(sid)
    return result


async def _answer(rt: Runtime, sid: str, state: State, task: Task) -> bool:
    call = state.pending_call(state.awaiting or "")
    mode = task.approval(call.name if call else "")
    if mode == "ignore":
        if state.awaiting_expires_at is None:
            return False
        await asyncio.sleep(max(0.0, state.awaiting_expires_at - time.time()) + 0.25)
        return True  # the next run records the expiry
    rt.approve(sid, state.awaiting or "", mode == "approve", by=APPROVER)
    return True


async def _drive_with_crashes(sid: str, crash: str, spec: EvalSpec, store: EventStore, task: Task,
                              slot: int | None = None, connectors_file: Path | None = None) -> tuple[int, int, str]:
    base = [sys.executable, "-m", "spool.cli", "--db", str(spec.out_dir / "spool.db"), "--data-dir", str(spec.out_dir)]
    if connectors_file is not None:
        base += ["--connectors", str(connectors_file)]
    base += ["resume", sid, "--model", spec.model_spec, "--quiet"]
    deadline = time.monotonic() + task.timeout_s
    crashes = resumes = 0
    env = dict(os.environ)
    if slot is not None:
        env["SPOOL_SLOT"] = str(slot)

    async def go() -> int:
        nonlocal crashes
        # The crash point stays armed until it fires: it may lie behind an approval.
        rc = await _run_proc(base, {**env, "SPOOL_CRASH_AT": crash} if not crashes else env, deadline)
        crashes += rc == -9
        return rc

    rc = await go()
    for _ in range(MAX_APPROVALS + 4):
        if time.monotonic() >= deadline:
            break
        status = (store.session(sid) or {}).get("status")
        if status == "awaiting_approval":
            # Approving needs no model: answer in this process, then let a fresh one continue.
            approver = Runtime(store, ScriptedModel([]), data_dir=spec.out_dir)
            if not await _answer(approver, sid, approver.state(sid), task):
                break
        elif rc == 0 or status not in ("running", "sleeping") or resumes >= 3:
            break  # finished (whatever the exit code), or out of resumes
        else:
            resumes += 1
        rc = await go()
    status = (store.session(sid) or {}).get("status", "unknown")
    return crashes, resumes, status


def _children(store: EventStore, sid: str) -> list[str]:
    return [row["id"] for row in store.sessions(where="id LIKE ?", args=(f"{sid}.%",), limit=1000)]


async def _run_proc(argv: list[str], env: dict[str, str], deadline: float) -> int:
    proc = await asyncio.create_subprocess_exec(*argv, env=env, stdout=asyncio.subprocess.DEVNULL,
                                                stderr=asyncio.subprocess.DEVNULL)
    try:
        return await asyncio.wait_for(proc.wait(), timeout=max(deadline - time.monotonic(), 1))
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1


def session_metrics(store: EventStore, sid: str) -> dict[str, Any]:
    """Metrics for one session, including any sub-agent sessions it spawned."""
    events = store.load(sid)
    state = replay(events)
    children = [row["id"] for row in store.sessions(where="id LIKE ?", args=(f"{sid}.%",), limit=1000)]
    usage = state.usage
    child_steps = 0
    for cid in children:
        child = replay(store.load(cid))
        child_steps += child.step
        usage = type(usage)(
            input_tokens=usage.input_tokens + child.usage.input_tokens,
            output_tokens=usage.output_tokens + child.usage.output_tokens,
            cached_tokens=usage.cached_tokens + child.usage.cached_tokens,
            cache_write_tokens=usage.cache_write_tokens + child.usage.cache_write_tokens,
            cost_usd=usage.cost_usd + child.usage.cost_usd,
            model_calls=usage.model_calls + child.usage.model_calls,
            model_latency_ms=usage.model_latency_ms + child.usage.model_latency_ms,
        )
    model_events = [e for e in events if e.type == ev.MODEL_RESPONSE]
    prefill = [e.data["timings"]["prompt_ms"] for e in model_events if (e.data.get("timings") or {}).get("prompt_ms") is not None]
    check = verify_render(events)
    final = state.final or {}
    return {
        "status": final.get("status"),
        "answer": (final.get("answer") or "")[:500],
        "steps": state.step,
        "child_sessions": len(children),
        "child_steps": child_steps,
        "tool_calls": state.tool_calls,
        "tool_errors": state.tool_errors,
        "denied_calls": state.denied_calls,
        "loop_warnings": state.loop_warnings,
        "model_errors": state.model_errors,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cached_tokens": usage.cached_tokens,
        "cost_usd": round(usage.cost_usd, 6),
        "model_calls": usage.model_calls,
        "model_latency_ms": round(usage.model_latency_ms),
        "prefill_ms_per_call": round(statistics.mean(prefill), 1) if prefill else None,
        "prefix_breaks": prefix_breaks(model_events),
        "folds": sum(1 for e in events if e.type == ev.CONTEXT_FOLDED),
        "compactions": state.compactions,
        "policy_triggers": sum(1 for e in events if e.type == ev.POLICY_DECISION and e.data.get("rule") == "rule_of_two"),
        "render_checked": check.checked,
        "render_mismatches": len(check.mismatches),
        "taint": sorted(state.taint),
    }
