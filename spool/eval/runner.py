"""Run tasks x trials under one config and record outcome-graded results.

Results are appended to <out>/results.jsonl as each trial finishes, so a long
run can be interrupted and resumed (finished trials are skipped).

Crash mode: the session is driven by `spool resume` subprocesses. The first one
runs with SPOOL_CRASH_AT and is SIGKILLed at that point; later ones resume from
the log until the session finishes. This exercises real process death, not a
simulated exception.
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
from ..context import prefix_breaks
from ..models import make_model
from ..models.base import ModelClient, PinnedModel
from ..replay import verify_render
from ..runtime import Runtime
from ..state import replay
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
    cfg = spec.config.replace(**overrides)

    rt = Runtime(store, model, data_dir=out, crash_hook=None)
    sid = rt.start(task.prompt, config=cfg, workspace=ws, fixtures=task.fixtures())
    started = time.perf_counter()
    harness_error = None
    crashes = resumes = 0
    status = "unknown"
    try:
        if crash:
            crashes, resumes, status = await _drive_with_crashes(sid, crash, spec, store, task.timeout_s, slot)
        else:
            result = await asyncio.wait_for(rt.run(sid), timeout=task.timeout_s)
            status = result.final_status or result.status
    except asyncio.TimeoutError:
        status = "timeout"
    except Exception as exc:  # recorded, never raised: one broken trial must not end the run
        harness_error = f"{type(exc).__name__}: {exc}"
        status = "harness_error"
    finally:
        await rt.aclose()
    wall_ms = (time.perf_counter() - started) * 1000

    ledger_dir = rt.ledger_dir(sid)
    passed, verify_out = await _verify(task, ws, ledger_dir)
    record = session_metrics(store, sid)
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


async def _drive_with_crashes(sid: str, crash: str, spec: EvalSpec, store: EventStore,
                              timeout: float, slot: int | None = None) -> tuple[int, int, str]:
    base = [sys.executable, "-m", "spool.cli", "--db", str(spec.out_dir / "spool.db"),
            "--data-dir", str(spec.out_dir), "resume", sid, "--model", spec.model_spec, "--quiet"]
    deadline = time.monotonic() + timeout
    crashes = resumes = 0
    env = dict(os.environ, SPOOL_CRASH_AT=crash)
    if slot is not None:
        env["SPOOL_SLOT"] = str(slot)
    rc = await _run_proc(base, env, deadline)
    if rc == -9:
        crashes += 1
    env.pop("SPOOL_CRASH_AT", None)
    while rc != 0 and time.monotonic() < deadline and resumes < 3:
        resumes += 1
        rc = await _run_proc(base, env, deadline)
    status = (store.session(sid) or {}).get("status", "unknown")
    return crashes, resumes, status


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
