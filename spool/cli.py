"""Command line interface.

    spool run "task" [--workspace DIR] [--model local] [--config FILE] [--set key=value ...]
    spool resume SESSION            continue a session (after a crash, approval or sleep)
    spool send SESSION "message"    follow-up message, then continue
    spool approve SESSION CALL_ID [--deny]
    spool sessions | show SESSION | view SESSION -o file.html | verify SESSION
    spool fork SESSION --at SEQ [--set key=value ...] [--run]
    spool tick                      fire due routines, wake sleeping sessions, expire lapsed approvals; run them
    spool daemon [--interval 15]    the same, forever, running sessions concurrently
    spool routine add NAME --task T [--cron EXPR] [--every S] [--watch DIR] [--webhook] [config flags]
    spool routine list | show NAME | fire NAME | pause NAME | resume NAME | rm NAME | apply FILE.toml
    spool eval --suite DIR [--config FILE] [--trials N] [--concurrency N] [--crash POINT,...]
    spool report RESULTS_DIR... [-o report.md]
    spool export RESULTS_DIR... -o data.jsonl [--all]
    spool web [--port 8765] [--model local]     local web app: start tasks, watch them live, approve, branch
    spool connectors [NAME...]      the configured MCP connectors' tools and the policy for each
    spool rules [add allow|ask|deny TOOL [--arg k=GLOB] | rm ID]   standing approval rules
    spool memory [add TEXT | review ID | forget ID]                 what the agent remembers across sessions
    (global: --connectors FILE; per session: --connector NAME)
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
from .store import EventStore
from .util import canonical_json, first_line, iso


# ------------------------------------------------------------------ helpers
def _parse_value(raw: str) -> Any:
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def explicit_overrides(args: argparse.Namespace) -> dict[str, Any]:
    """Only the config fields given on the command line (--set and the shorthand flags)."""
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
    if getattr(args, "connector", None):
        changes["connectors"] = tuple(dict.fromkeys(args.connector))
    if getattr(args, "memory", False):
        changes["memory"] = True
    return changes


def build_config(args: argparse.Namespace, base: AgentConfig | None = None) -> AgentConfig:
    cfg = AgentConfig.from_toml(args.config) if getattr(args, "config", None) else (base or AgentConfig())
    changes = explicit_overrides(args)
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


def _connectors(args):
    from .connectors import ConnectorManager

    path = ConnectorManager.default_path(args.data_dir, getattr(args, "connectors", None))
    return ConnectorManager.from_file(path, data_dir=args.data_dir)


def _runtime(args, store: EventStore, model_spec: str) -> Runtime:
    return Runtime(store, make_model(model_spec), data_dir=args.data_dir, on_event=_printer(args.quiet),
                   connectors=_connectors(args))


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
    if args.always and not args.deny:
        state = rt.state(args.session)
        verdict = state.verdicts.get(args.call_id) or {}
        if verdict.get("rule") != "confirm":
            raise SystemExit("only steps that ask every time can be always allowed; this one asks because of the "
                             "Rule of Two (what the run has already read and accessed)")
        rule = rt.rulebook.add("allow", verdict["tool"], note=f"from an approval in {args.session}")
        print(f"added rule {rule.id}: {rule.describe()}")
    rt.approve(args.session, args.call_id, not args.deny, by=os.environ.get("USER", "user"), note=args.note or "")
    print(f"{'denied' if args.deny else 'approved'} {args.call_id}; continue with: spool resume {args.session}")
    return 0


def cmd_rules(args) -> int:
    from .rules import RuleBook, RuleError

    book = RuleBook(_store(args))
    try:
        if args.rules_command == "add":
            pairs = dict(item.split("=", 1) for item in args.arg or [])
            rule = book.add(args.effect, args.tool, pairs, args.note or "")
            print(f"added {rule.id}: {rule.describe()}")
        elif args.rules_command == "rm":
            book.remove(args.id)
            print(f"removed {args.id}")
        else:
            rules = book.rules()
            for rule in rules:
                print(f"{rule.id:<18} {rule.describe()}" + (f"  ({rule.note})" if rule.note else ""))
            if not rules:
                print("no standing rules; steps ask as the policy says")
    except (RuleError, KeyError, ValueError) as exc:
        raise SystemExit(str(exc).strip("'\""))
    return 0


def cmd_memory(args) -> int:
    from .memory import MemoryBook

    book = MemoryBook(_store(args))
    try:
        if args.memory_command == "add":
            memory, _ = book.save(args.text, source={"by": "user"}, reviewed=True)
            print(f"remembered {memory.id}")
        elif args.memory_command == "review":
            book.review(args.id)
        elif args.memory_command == "forget":
            book.forget(args.id)
        else:
            items = book.all()
            for m in items:
                origin = f"from {m.source['session_id']}" if m.source.get("session_id") else "added by you"
                flag = "  UNREVIEWED (written after reading untrusted content)" if m.carries else ""
                print(f"{m.id}  {m.text}  ({origin}){flag}")
            if not items:
                print("nothing remembered yet")
    except (KeyError, ValueError) as exc:
        raise SystemExit(str(exc).strip("'\""))
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


def _scheduler(args, store: EventStore):
    from .models.scripted import ScriptedModel
    from .routines import Scheduler

    rt = Runtime(store, ScriptedModel([]), data_dir=args.data_dir)  # firing only starts sessions; no model needed
    model = getattr(args, "model", None)
    return Scheduler(rt, base_config=AgentConfig(model=model) if model else None)


def due_work(store: EventStore, scheduler) -> tuple[list[str], list[str]]:
    """(sessions started by routines that fired now, sessions whose wake-up or approval deadline passed)."""
    fired = [r.session_id for r in scheduler.tick() if r.session_id and r.started]
    due = [sid for sid in store.due_sessions(time.time()) if sid not in fired]
    return fired, due


def cmd_tick(args) -> int:
    store = _store(args)
    fired, due = due_work(store, _scheduler(args, store))
    print(f"{len(fired)} routine run(s) started, {len(due)} waiting session(s) due")
    code = 0
    for sid in fired + due:
        rt = _runtime(args, store, _session_model(store, sid, args.model))
        code = max(code, asyncio.run(_drive(rt, sid)))
    return code


def cmd_daemon(args) -> int:
    """Tick every --interval seconds and run what is due, several sessions at a time."""
    store = _store(args)
    scheduler = _scheduler(args, store)
    rt = _runtime(args, store, args.model or "local")
    running: dict[str, asyncio.Task] = {}

    async def loop() -> None:
        # Routine runs that a previous daemon left mid-way continue from their logs.
        for row in store.sessions(where="status = 'running' AND id LIKE 'r-%'", limit=500):
            running[row["id"]] = asyncio.create_task(rt.run(row["id"]))
        print(f"spool daemon: ticking every {args.interval:g}s (model: {args.model or 'local'}, data: {args.data_dir})",
              flush=True)
        while True:
            fired, due = due_work(store, scheduler)
            for sid in fired + due:
                if sid not in running or running[sid].done():
                    running[sid] = asyncio.create_task(rt.run(sid))
            for sid, task in list(running.items()):
                if task.done():
                    running.pop(sid)
                    exc = task.exception()
                    if exc is not None:
                        print(f"  ✗ {sid}: {type(exc).__name__}: {exc}", flush=True)
            await asyncio.sleep(args.interval)

    try:
        asyncio.run(loop())
    except KeyboardInterrupt:
        pass
    return 0


# ----------------------------------------------------------------- routines
def _routine_spec(args):
    from .routines import RoutineSpec, Trigger

    triggers = []
    if args.cron:
        triggers.append(Trigger("cron", cron=args.cron))
    if args.every:
        triggers.append(Trigger("interval", every_s=float(args.every)))
    if args.watch:
        triggers.append(Trigger("file", path=args.watch, pattern=args.pattern or "*"))
    if args.webhook:
        triggers.append(Trigger("webhook"))
    return RoutineSpec(name=args.name, task=args.task, triggers=tuple(triggers), config=explicit_overrides(args),
                       workspace=args.workspace, timezone=args.tz, overlap=args.overlap, misfire=args.misfire,
                       approval_ttl_s=args.ttl)


def _print_routine(summary: dict[str, Any]) -> None:
    status = "on " if summary["enabled"] else "off"
    nxt = iso(summary["next_at"]) if summary.get("next_at") else "-"
    last = f"{summary['last_session']} ({summary['last_status']})" if summary.get("last_session") else "never run"
    print(f"{summary['name']:<22} {status}  {', '.join(summary['triggers']) or 'manual only':<34} next {nxt}  last {last}")


def cmd_routine(args) -> int:
    from .routines import RoutineError, specs_from_toml, summarize_routine

    store = _store(args)
    scheduler = _scheduler(args, store)
    now = time.time()
    try:
        if args.routine_command == "add":
            state = scheduler.save(_routine_spec(args))
            _print_routine(summarize_routine(state, store, now, scheduler))
        elif args.routine_command == "apply":
            with open(args.file, "rb") as fh:
                specs = specs_from_toml(tomllib.load(fh))
            for spec in specs:
                _print_routine(summarize_routine(scheduler.save(spec), store, now, scheduler))
        elif args.routine_command == "list":
            rows = store.routines()
            for row in rows:
                _print_routine(summarize_routine(scheduler.state(row["name"]), store, now, scheduler))
            if not rows:
                print("no routines; add one with: spool routine add NAME --task '...' --cron '0 9 * * *'")
        elif args.routine_command == "show":
            for e in store.load(f"routine:{args.name}"):
                print(f"{e.seq:>4} {iso(e.ts)} {e.type:<16} {_short({k: v for k, v in e.data.items() if k != 'spec'}, 140)}")
        elif args.routine_command == "fire":
            payload = json.loads(args.payload) if args.payload else None
            result = scheduler.fire(args.name, payload=payload, key=args.key, kind="manual")
            if not result.session_id:
                print(f"not started: {result.skipped}")
                return 1
            print(f"started {result.session_id}" if result.started else f"already ran as {result.session_id}")
            rt = _runtime(args, store, _session_model(store, result.session_id, args.model))
            return asyncio.run(_drive(rt, result.session_id))
        elif args.routine_command == "pause":
            scheduler.pause(args.name)
        elif args.routine_command == "resume":
            scheduler.resume(args.name)
        elif args.routine_command == "rm":
            scheduler.delete(args.name)
    except (RoutineError, KeyError) as exc:
        raise SystemExit(str(exc).strip("'\""))
    return 0


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
    runtime = Runtime(store, make_model(cfg.model), data_dir=args.data_dir, connectors=_connectors(args))
    app = create_app(runtime, default_config=cfg, read_only=args.read_only)
    mode = "read-only, " if args.read_only else ""
    print(f"Spool is running at http://{args.host}:{args.port}  ({mode}model: {cfg.model}, data: {args.data_dir})")
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


def cmd_connectors(args) -> int:
    """List each configured connector's tools and the policy Spool derives for them."""
    manager = _connectors(args)
    if manager is None:
        raise SystemExit("no connectors configured: pass --connectors FILE or create <data-dir>/connectors.toml "
                         "(see examples/connectors.toml)")
    names = args.names or manager.names()

    async def listing() -> dict[str, Any]:
        probe = Path(args.data_dir) / "ledgers" / "_connectors_probe"
        try:
            return await manager.snapshot(names, workspace=Path.cwd(), state_dir=probe)
        finally:
            await manager.aclose()

    snap = asyncio.run(listing())
    if args.json:
        print(json.dumps(snap, indent=2, ensure_ascii=False))
        return 0
    for name in names:
        if name in snap["errors"]:
            print(f"{name}: unavailable: {snap['errors'][name]}")
            continue
        info = snap["servers"][name]
        server = info.get("server") or {}
        print(f"{name}  ({server.get('name', '?')} {server.get('version', '')}, MCP {info['version']}, {info['era']} era)")
        for t in [t for t in snap["tools"] if t["connector"] == name]:
            flags = [("asks every time" if t["confirm"] else ""), ("exactly once" if t["ledgered"] else
                     "re-run after a crash" if t["idempotent"] else "reported if interrupted")]
            props = ", ".join(t["properties"]) or "no risk properties"
            print(f"  {t['name']:<28} {props:<38} {'; '.join(f for f in flags if f)}")
    return 1 if snap["errors"] else 0


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
    p.add_argument("--connector", action="append", metavar="NAME", help="give the session a connector's tools (repeatable)")
    p.add_argument("--memory", action="store_true", help="give the session cross-session memory")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spool", description="Event-sourced runtime for long-running agents")
    parser.add_argument("--data-dir", default=os.environ.get("SPOOL_HOME", ".spool"))
    parser.add_argument("--db", help="event store path (default: <data-dir>/spool.db)")
    parser.add_argument("--connectors", help="connector definitions (default: $SPOOL_CONNECTORS or <data-dir>/connectors.toml)")
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
    p.add_argument("--always", action="store_true", help="also allow this tool from now on (confirmations only)")
    p.add_argument("--note")
    p.set_defaults(func=cmd_approve)

    p = sub.add_parser("memory", help="what the agent remembers across sessions")
    msub = p.add_subparsers(dest="memory_command")
    r = msub.add_parser("add")
    r.add_argument("text")
    for name in ("review", "forget"):
        r = msub.add_parser(name)
        r.add_argument("id")
    msub.add_parser("list")
    p.set_defaults(func=cmd_memory)

    p = sub.add_parser("rules", help="standing approval rules: always allow, always ask, never")
    rsub = p.add_subparsers(dest="rules_command")
    r = rsub.add_parser("add")
    r.add_argument("effect", choices=["allow", "ask", "deny"])
    r.add_argument("tool", help="tool name or glob, e.g. mail__send_message or shop__*")
    r.add_argument("--arg", action="append", metavar="NAME=GLOB", help="only when an argument matches (repeatable)")
    r.add_argument("--note")
    r = rsub.add_parser("rm")
    r.add_argument("id")
    rsub.add_parser("list")
    p.set_defaults(func=cmd_rules)

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

    p = sub.add_parser("tick", help="fire due routines and run every session that is due (cron-friendly)")
    p.add_argument("--model")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_tick)

    p = sub.add_parser("daemon", help="tick forever and run due sessions concurrently")
    p.add_argument("--interval", type=float, default=15.0)
    p.add_argument("--model")
    p.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_daemon)

    p = sub.add_parser("routine", help="saved tasks that run on a schedule, on new files, or on a webhook")
    rsub = p.add_subparsers(dest="routine_command", required=True)
    r = rsub.add_parser("add", help="create or update a routine")
    r.add_argument("name")
    r.add_argument("--task", required=True, help="may use {date} {time} {now} {files} {payload}")
    r.add_argument("--cron", help="e.g. '0 9 * * 1-5' (weekdays at 09:00)")
    r.add_argument("--every", type=float, help="interval in seconds (>= 60)")
    r.add_argument("--watch", help="folder in the routine workspace; new files start a run")
    r.add_argument("--pattern", help="glob for --watch, e.g. '*.csv'")
    r.add_argument("--webhook", action="store_true", help="allow POST /api/routines/NAME/fire")
    r.add_argument("--tz", help="IANA time zone for --cron (default: this machine's)")
    r.add_argument("--ttl", type=float, default=600.0, help="seconds before an unanswered approval expires")
    r.add_argument("--overlap", choices=["skip", "allow"], default="skip")
    r.add_argument("--misfire", choices=["run_once", "skip"], default="run_once")
    r.add_argument("--workspace", help="default: <data-dir>/routines/NAME, kept across runs")
    _add_config_flags(r)
    r = rsub.add_parser("apply", help="create or update the [[routine]] tables of a TOML file")
    r.add_argument("file")
    rsub.add_parser("list", help="routines, their triggers and next run")
    for name in ("show", "pause", "resume", "rm"):
        r = rsub.add_parser(name)
        r.add_argument("name")
    r = rsub.add_parser("fire", help="run a routine now and follow it")
    r.add_argument("name")
    r.add_argument("--payload", help="JSON available to the task as {payload}")
    r.add_argument("--key", help="delivery key; firing again with the same key does not run twice")
    r.add_argument("--model")
    r.add_argument("--quiet", "-q", action="store_true")
    p.set_defaults(func=cmd_routine)

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

    p = sub.add_parser("connectors", help="list the configured connectors' tools and the policy for each")
    p.add_argument("names", nargs="*")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_connectors)

    p = sub.add_parser("export", help="export trajectories as chat-format JSONL for fine-tuning")
    p.add_argument("results", nargs="+")
    p.add_argument("-o", "--output", required=True)
    p.add_argument("--all", action="store_true", help="include failed trials")
    p.set_defaults(func=cmd_export)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
