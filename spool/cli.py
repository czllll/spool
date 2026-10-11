"""Command line interface.

    spool run "task" [--workspace DIR] [--model local] [--config FILE] [--set key=value ...]
    spool resume SESSION            continue a session (after a crash, approval or sleep)
    spool send SESSION "message"    follow-up message, then continue
    spool approve SESSION CALL_ID [--deny]
    spool sessions | show SESSION | view SESSION -o file.html | verify SESSION
    spool fork SESSION --at SEQ [--set key=value ...] [--run]
    spool tick                      resume every sleeping session whose wake-up time has passed
    spool eval --suite DIR [--config FILE] [--trials N] [--concurrency N] [--crash POINT,...]
    spool report RESULTS_DIR... [-o report.md]
    spool export RESULTS_DIR... -o data.jsonl [--all]
    spool web [--port 8765] [--model local]     local web app: start tasks, watch them live, approve, branch
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import tomllib
from pathlib import Path
from typing import Any

from . import events as ev
from .config import AgentConfig
from .events import Event
from .models import make_model
from .replay import verify_render
from .runtime import Runtime
from .state import replay
from .store import EventStore
from .util import canonical_json, first_line, iso


# ------------------------------------------------------------------ helpers
def _parse_value(raw: str) -> Any:
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def build_config(args: argparse.Namespace, base: AgentConfig | None = None) -> AgentConfig:
    cfg = AgentConfig.from_toml(args.config) if getattr(args, "config", None) else (base or AgentConfig())
    changes: dict[str, Any] = {}
    for item in getattr(args, "set", None) or []:
        key, _, value = item.partition("=")
        changes[key.strip()] = _parse_value(value.strip())
    for flag in ("model", "approval", "sandbox", "max_steps", "context_strategy", "policy"):
        value = getattr(args, flag, None)
        if value is not None:
            changes[flag] = value
    if getattr(args, "tools", None):
        changes["tools"] = tuple(t.strip() for t in args.tools.split(",") if t.strip())
    if changes:
        merged = cfg.to_dict()
        merged.update(changes)
        cfg = AgentConfig.from_dict(merged)
    return cfg


def _short(obj: Any, limit: int = 120) -> str:
    text = obj if isinstance(obj, str) else canonical_json(obj)
    text = text.replace("\n", "⏎ ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe(event: Event) -> str | None:
    d = event.data
    t = event.type
    if t == ev.SESSION_STARTED:
        cfg = d.get("config", {})
        return (f"● session {event.session_id}  model={cfg.get('model')}  sandbox={cfg.get('sandbox')}  "
                f"context={cfg.get('context_strategy')}  policy={cfg.get('policy')}/{cfg.get('approval')}\n"
                f"  workspace {d.get('workspace')}")
    if t == ev.USER_MESSAGE:
        return f"▶ user: {_short(d['text'], 300)}"
    if t == ev.MODEL_RESPONSE:
        msg = d["message"]
        lines = []
        if msg.get("content"):
            lines.append(f"◆ assistant: {_short(msg['content'], 400)}")
        for c in msg.get("tool_calls") or []:
            lines.append(f"  → {c['name']}({_short(c.get('arguments') or c.get('raw_arguments'), 160)})")
        u = d.get("usage", {})
        lines.append(f"    [{u.get('input_tokens', 0)} in · {u.get('cached_tokens', 0)} cached · "
                     f"{u.get('output_tokens', 0)} out · {d.get('latency_ms', 0) / 1000:.1f}s]")
        return "\n".join(lines)
    if t == ev.MODEL_ERROR:
        return f"  ✗ model error: {_short(d.get('error'), 200)}"
    if t == ev.POLICY_DECISION and d.get("verdict") != "allow":
        return f"  ⛔ policy {d['verdict']} {d['tool']}: {d.get('rule')}"
    if t == ev.APPROVAL_REQUESTED:
        return (f"  ⏸ approval needed for {d['tool']}({_short(d.get('arguments'), 100)})\n"
                f"    {d.get('reason')}\n    approve: spool approve {event.session_id} {d['call_id']}   "
                f"deny: spool approve {event.session_id} {d['call_id']} --deny")
    if t == ev.APPROVAL_DECIDED:
        return f"  ✓ approval {'granted' if d['approved'] else 'denied'} by {d.get('by')}"
    if t == ev.TOOL_COMPLETED:
        status = "ok" if d.get("ok") else f"{d.get('kind')} error: {_short(d.get('error'), 160)}"
        gist = first_line(d.get("output") or "", 120)
        extra = f"  (offloaded {d['full_chars']} chars)" if d.get("offloaded_path") else ""
        return f"  ← {d['name']} {status}" + (f" · {gist}" if gist and d.get("ok") else "") + extra
    if t == ev.CONTEXT_FOLDED:
        return f"  ⇤ context folded through step {d['through_step']} (≈{d.get('estimated_tokens_before')} tokens before)"
    if t == ev.CONTEXT_COMPACTED:
        return f"  ⇤ context compacted through event {d['upto_seq']}"
    if t == ev.NOTE:
        return f"  · {d.get('kind')} {_short({k: v for k, v in d.items() if k != 'kind'}, 100)}"
    if t == ev.SESSION_FINISHED:
        u = d.get("usage", {})
        return (f"■ finished: {d['status']} after {d.get('steps')} steps · {u.get('input_tokens', 0)} in "
                f"({u.get('cached_tokens', 0)} cached) · {u.get('output_tokens', 0)} out"
                + (f"\n{d['answer']}" if d.get("answer") else f"\n{d.get('reason', '')}"))
    return None


def _printer(quiet: bool):
    if quiet:
        return None

    def on_event(event: Event) -> None:
        text = describe(event)
        if text:
            print(text, flush=True)

    return on_event


def _store(args) -> EventStore:
    return EventStore(Path(args.db) if args.db else Path(args.data_dir) / "spool.db")


def _runtime(args, store: EventStore, model_spec: str) -> Runtime:
    return Runtime(store, make_model(model_spec), data_dir=args.data_dir, on_event=_printer(args.quiet))


def _session_model(store: EventStore, sid: str, override: str | None) -> str:
    if override:
        return override
    events = store.load(sid, upto_seq=0)
    if not events:
        raise SystemExit(f"no session {sid}")
    return events[0].data.get("config", {}).get("model", "local")


async def _drive(rt: Runtime, sid: str) -> int:
    result = await rt.run(sid)
    await rt.aclose()
    if result.status == "awaiting_approval":
        return 3
    if result.status == "sleeping":
        print(f"  ☾ sleeping until {iso(result.state.wake_at)}; run `spool tick` or `spool resume {sid}` later")
        return 4
    return 0 if result.final_status == "completed" else 1


# ----------------------------------------------------------------- commands
def cmd_run(args) -> int:
    store = _store(args)
    cfg = build_config(args)
    rt = _runtime(args, store, cfg.model)
    workspace = Path(args.workspace).resolve() if args.workspace else None
    sid = rt.start(args.task, config=cfg, workspace=workspace)
    return asyncio.run(_drive(rt, sid))


def cmd_resume(args) -> int:
    store = _store(args)
    rt = _runtime(args, store, _session_model(store, args.session, args.model))
    return asyncio.run(_drive(rt, args.session))


def cmd_send(args) -> int:
    store = _store(args)
    rt = _runtime(args, store, _session_model(store, args.session, args.model))
    rt.send(args.session, args.message)
    return asyncio.run(_drive(rt, args.session))


def cmd_approve(args) -> int:
    store = _store(args)
    from .models.scripted import ScriptedModel

    rt = Runtime(store, ScriptedModel([]), data_dir=args.data_dir)  # approving needs no model
    rt.approve(args.session, args.call_id, not args.deny, by=os.environ.get("USER", "user"), note=args.note or "")
    print(f"{'denied' if args.deny else 'approved'} {args.call_id}; continue with: spool resume {args.session}")
    return 0


def cmd_sessions(args) -> int:
    store = _store(args)
    for row in store.sessions(limit=args.limit):
        print(f"{row['id']:<28} {row['status']:<18} {row['n_events']:>4} ev  {iso(row['updated_at'])}  "
              f"{first_line(row['task'], 60)}")
    return 0


def cmd_show(args) -> int:
    store = _store(args)
    events = store.load(args.session)
    if not events:
        raise SystemExit(f"no session {args.session}")
    for e in events:
        if args.json:
            print(json.dumps({"seq": e.seq, "type": e.type, "ts": e.ts, "data": e.data}, ensure_ascii=False))
        else:
            text = describe(e)
            if text:
                print(f"{e.seq:>4} {text}")
    return 0


def cmd_view(args) -> int:
    from .viewer import render_html

    store = _store(args)
    events = store.load(args.session)
    if not events:
        raise SystemExit(f"no session {args.session}")
    out = Path(args.output or f"{args.session}.html")
    children = {row["id"]: store.load(row["id"]) for row in
                store.sessions(where="id LIKE ?", args=(f"{args.session}.%",), limit=100)}
    out.write_text(render_html(events, children), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def cmd_verify(args) -> int:
    store = _store(args)
    check = verify_render(store.load(args.session))
    print(f"re-rendered {check.checked} model requests from the log: "
          + ("all match the recorded digests" if check.ok else f"{len(check.mismatches)} mismatches"))
    for m in check.mismatches[:10]:
        print(f"  seq {m['seq']}: first difference in {m['first_difference']}")
    return 0 if check.ok else 1


def cmd_fork(args) -> int:
    store = _store(args)
    base = AgentConfig.from_dict(store.load(args.session, upto_seq=0)[0].data["config"])
    cfg = build_config(args, base=base)
    rt = _runtime(args, store, cfg.model)
    sid = rt.fork(args.session, args.at, config=cfg)
    print(f"forked {args.session}@{args.at} -> {sid}")
    if args.run:
        return asyncio.run(_drive(rt, sid))
    return 0


def cmd_tick(args) -> int:
    store = _store(args)
    now = time.time()
    due = []
    for row in store.sessions(where="status = 'sleeping'", limit=1000):
        state = replay(store.load(row["id"]))
        if state.wake_at is not None and state.wake_at <= now:
            due.append(row["id"])
    print(f"{len(due)} sleeping session(s) due")
    code = 0
    for sid in due:
        rt = _runtime(args, store, _session_model(store, sid, args.model))
        code = max(code, asyncio.run(_drive(rt, sid)))
    return code


def cmd_eval(args) -> int:
    from .eval.report import render_report
    from .eval.runner import EvalSpec, run_eval
    from .eval.task import load_suite

    tasks = []
    for suite in args.suite:
        tasks.extend(load_suite(suite, args.tasks))
    if not tasks:
        raise SystemExit("no tasks found")
    cfg = build_config(args)
    name = args.name or (Path(args.config).stem if args.config else "default")
    out = Path(args.out or f"evals/results/{name}")
    spec = EvalSpec(name=name, config=cfg, model_spec=cfg.model, trials=args.trials, concurrency=args.concurrency,
                    crash_points=[c for c in (args.crash or "").split(",") if c], out_dir=out,
                    config_source=args.config)
    asyncio.run(run_eval(tasks, spec))
    report = render_report([out], title=f"Eval: {name}")
    (out / "report.md").write_text(report, encoding="utf-8")
    print(report)
    return 0


def cmd_report(args) -> int:
    from .eval.report import render_report

    report = render_report(args.results, title=args.title)
    if args.output:
        Path(args.output).write_text(report, encoding="utf-8")
        print(f"wrote {args.output}")
    else:
        print(report)
    return 0


def cmd_web(args) -> int:
    try:
        import uvicorn

        from .web.app import create_app
    except ImportError:
        raise SystemExit("the web app needs its extra: pip install -e '.[web]'")
    store = _store(args)
    cfg = build_config(args)
    runtime = Runtime(store, make_model(cfg.model), data_dir=args.data_dir)
    app = create_app(runtime, default_config=cfg, read_only=args.read_only)
    mode = "read-only, " if args.read_only else ""
    print(f"Spool is running at http://{args.host}:{args.port}  ({mode}model: {cfg.model}, data: {args.data_dir})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_export(args) -> int:
    from .export import export_results

    n = export_results(args.results, Path(args.output), passed_only=not args.all)
    print(f"wrote {n} trajectories to {args.output}")
    return 0


# -------------------------------------------------------------------- main
def _add_config_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", help="TOML file with AgentConfig fields")
    p.add_argument("--set", action="append", metavar="KEY=VALUE", help="override a config field (repeatable)")
    p.add_argument("--model", help="model spec, e.g. local, dashscope:qwen-plus, anthropic:claude-opus-5-5")
    p.add_argument("--approval", choices=["auto_approve", "auto_deny", "human"])
    p.add_argument("--policy", choices=["off", "rule_of_two"])
    p.add_argument("--sandbox", choices=["auto", "process", "seatbelt", "docker"])
    p.add_argument("--context-strategy", dest="context_strategy", choices=["full", "offload", "fold", "summarize"])
    p.add_argument("--max-steps", dest="max_steps", type=int)
    p.add_argument("--tools", help="comma-separated tool allow-list")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spool", description="Event-sourced runtime for long-running agents")
    parser.add_argument("--data-dir", default=os.environ.get("SPOOL_HOME", ".spool"))
    parser.add_argument("--db", help="event store path (default: <data-dir>/spool.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="start a session and run it")
    p.add_argument("task")
    p.add_argument("--workspace", "-w")
    p.add_argument("--quiet", "-q", action="store_true")
    _add_config_flags(p)
    p.set_defaults(func=cmd_run)

    for name, func, helptext in [("resume", cmd_resume, "continue a session")]:
        p = sub.add_parser(name, help=helptext)
        p.add_argument("session")
        p.add_argument("--model")
        p.add_argument("--quiet", "-q", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("send", help="send a follow-up message and continue")
    p.add_argument("session")
    p.add_argument("message")
    p.add_argument("--model")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_send)

    p = sub.add_parser("approve", help="approve or deny a pending tool call")
    p.add_argument("session")
    p.add_argument("call_id")
    p.add_argument("--deny", action="store_true")
    p.add_argument("--note")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("sessions", help="list sessions")
    p.add_argument("--limit", type=int, default=30)
    p.set_defaults(func=cmd_sessions)

    p = sub.add_parser("show", help="print a session's events")
    p.add_argument("session")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("view", help="write a self-contained HTML timeline of a session")
    p.add_argument("session")
    p.add_argument("-o", "--output")
    p.set_defaults(func=cmd_view)

    p = sub.add_parser("verify", help="re-render every recorded request from the log and compare digests")
    p.add_argument("session")
    p.set_defaults(func=cmd_verify)

    p = sub.add_parser("fork", help="branch a session at an event")
    p.add_argument("session")
    p.add_argument("--at", type=int, required=True)
    p.add_argument("--run", action="store_true")
    p.add_argument("--quiet", "-q", action="store_true")
    _add_config_flags(p)
    p.set_defaults(func=cmd_fork)

    p = sub.add_parser("tick", help="resume sleeping sessions that are due")
    p.add_argument("--model")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("eval", help="run an eval suite")
    p.add_argument("--suite", action="append", required=True)
    p.add_argument("--tasks", help="only tasks whose id contains this text")
    p.add_argument("--name")
    p.add_argument("--out")
    p.add_argument("--trials", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=1)
    p.add_argument("--crash", help="comma-separated SPOOL_CRASH_AT specs, cycled over trials")
    _add_config_flags(p)
    p.set_defaults(func=cmd_eval)

    p = sub.add_parser("report", help="markdown report for one or more result dirs")
    p.add_argument("results", nargs="+")
    p.add_argument("-o", "--output")
    p.add_argument("--title", default="Eval report")
    p.set_defaults(func=cmd_report)

    p = sub.add_parser("web", help="open the local web app")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--read-only", action="store_true", help="browse sessions without running or changing anything")
    _add_config_flags(p)
    p.set_defaults(func=cmd_web)

    p = sub.add_parser("export", help="export trajectories as chat-format JSONL for fine-tuning")
    p.add_argument("results", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--all", action="store_true", help="include failed trials")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
