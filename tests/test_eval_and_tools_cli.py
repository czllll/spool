"""Eval runner, metrics, replay checks, viewer, export, RL env and the CLI, all offline."""

import asyncio
import json
import textwrap
from pathlib import Path

import pytest

from spool import events as ev
from spool.cli import main as cli_main
from spool.config import AgentConfig
from spool.env import SpoolEnv
from spool.eval.metrics import pass_at_k, pass_hat_k, summarize, wilson_interval
from spool.eval.report import render_report
from spool.eval.runner import EvalSpec, run_eval
from spool.eval.task import load_task
from spool.export import export_results
from spool.models.scripted import ScriptedModel, call, reply
from spool.replay import verify_render
from spool.runtime import Runtime
from spool.store import EventStore
from spool.viewer import render_html

from .conftest import cfg, run


def make_task(tmp_path: Path, name="hello") -> Path:
    d = tmp_path / "tasks" / "unit" / name
    (d / "setup").mkdir(parents=True)
    (d / "setup" / "input.txt").write_text("spool\n")
    (d / "task.toml").write_text('prompt = "Copy input.txt to output.txt"\nmax_steps = 5\n')
    (d / "verify.py").write_text(textwrap.dedent("""
        import pathlib, sys
        ws = pathlib.Path(sys.argv[1])
        out = ws / "output.txt"
        if out.exists() and out.read_text() == "spool\\n":
            print("ok"); sys.exit(0)
        print("output.txt missing or wrong"); sys.exit(1)
    """))
    return d


# ---------------------------------------------------------------- metrics
def test_pass_metrics():
    assert pass_at_k(3, 0, 3) == 0.0 and pass_at_k(3, 1, 3) == 1.0
    assert pass_hat_k(3, 3, 3) == 1.0 and pass_hat_k(3, 2, 3) == 0.0
    assert pass_hat_k(5, 4, 3) == pytest.approx(4 / 10)   # C(4,3)/C(5,3)
    assert pass_at_k(5, 1, 3) == pytest.approx(1 - 4 / 10)
    lo, hi = wilson_interval(8, 10)
    assert 0.44 < lo < 0.5 and 0.94 < hi < 0.98


def test_summary_and_failure_categories():
    rows = [
        {"task_id": "a", "passed": True, "status": "completed", "steps": 3, "input_tokens": 100, "cached_tokens": 80},
        {"task_id": "a", "passed": False, "status": "max_steps", "steps": 10, "input_tokens": 100, "cached_tokens": 0},
        {"task_id": "a", "passed": False, "status": "completed", "steps": 4, "input_tokens": 100, "cached_tokens": 50},
    ]
    s = summarize(rows)
    assert s["success_rate"] == pytest.approx(1 / 3) and s["pass^3"] == 0.0 and s["pass@3"] == 1.0
    assert s["cache_reuse"] == pytest.approx(130 / 300)
    assert s["failures"] == {"ran_out_of_steps": 1, "wrong_result_claimed_done": 1}


# ---------------------------------------------------------------- runner
def test_run_eval_offline_with_scripted_model(tmp_path):
    task = load_task(make_task(tmp_path))
    model = ScriptedModel([
        call("bash", command="cp input.txt output.txt", call_id="c1"), reply("copied"),   # trial 0 passes
        reply("I did nothing"),                                                         # trial 1 fails
    ])
    spec = EvalSpec(name="unit", config=cfg(), trials=2, concurrency=1, out_dir=tmp_path / "out")
    results = run(run_eval([task], spec, model=model, progress=lambda _: None))
    by_trial = {r["trial"]: r for r in results}
    assert by_trial[0]["passed"] and not by_trial[1]["passed"]
    assert by_trial[0]["render_mismatches"] == 0 and by_trial[0]["render_checked"] == 2
    summary = json.loads((tmp_path / "out" / "summary.json").read_text())
    assert summary["passed"] == 1 and summary["trials"] == 2
    # resuming the run skips finished trials
    again = run(run_eval([task], spec, model=ScriptedModel([]), progress=lambda _: None))
    assert len(again) == 2
    report = render_report([tmp_path / "out"], title="unit")
    assert "| unit | 2 | 50%" in report and "unit/hello" in report


# ---------------------------------------------------------------- replay
def test_verify_render_catches_hidden_inputs(tmp_path, workspace):
    store = EventStore(tmp_path / "db.sqlite")
    model = ScriptedModel([call("list_files", call_id="c1"), call("list_files", call_id="c2"), reply("done")])
    rt = Runtime(store, model, data_dir=tmp_path / "d")
    sid = rt.start("t", config=cfg(unstable_prefix=True), workspace=workspace)
    run(rt.run(sid))
    events = store.load(sid)
    assert verify_render(events).ok  # the clock is recorded with each request, so even this replays
    # Tamper with a recorded tool result: re-rendering no longer matches what the model saw.
    tampered = [e if e.type != ev.TOOL_COMPLETED else type(e)(e.session_id, e.seq, e.type,
                {**e.data, "output": e.data["output"] + "x"}, e.ts) for e in events]
    check = verify_render(tampered)
    assert not check.ok and check.mismatches[0]["first_difference"].startswith("message")
    run(rt.aclose())


# ---------------------------------------------------------------- viewer / export
def test_viewer_and_export(tmp_path):
    task = load_task(make_task(tmp_path))
    model = ScriptedModel([call("bash", command="cp input.txt output.txt", call_id="c1"), reply("copied <ok>")])
    out = tmp_path / "out"
    run(run_eval([task], EvalSpec(name="v", config=cfg(), trials=1, out_dir=out), model=model, progress=lambda _: None))
    store = EventStore(out / "spool.db")
    sid = json.loads((out / "results.jsonl").read_text().splitlines()[0])["session_id"]
    page = render_html(store.load(sid))
    assert "<!doctype html>" in page and "copied &lt;ok&gt;" in page and f"spool fork {sid} --at" in page
    n = export_results([out], tmp_path / "sft.jsonl")
    line = json.loads((tmp_path / "sft.jsonl").read_text())
    assert n == 1 and line["meta"]["passed"] is True
    roles = [m["role"] for m in line["messages"]]
    assert roles == ["system", "user", "assistant", "tool", "assistant"]
    assert line["messages"][2]["tool_calls"][0]["function"]["name"] == "bash"


# ---------------------------------------------------------------- RL env
def test_spool_env_episode(tmp_path):
    task = load_task(make_task(tmp_path))

    async def episode():
        env = SpoolEnv(task, config=AgentConfig(sandbox="process"), data_dir=tmp_path / "rollouts")
        obs = await env.reset()
        assert obs["messages"][0]["content"] == "Copy input.txt to output.txt"
        assert any(t["name"] == "bash" for t in obs["tools"])
        obs, reward, done, info = await env.step(
            {"tool_calls": [{"name": "bash", "arguments": {"command": "cp input.txt output.txt"}}]})
        assert not done and obs["messages"][-1]["role"] == "tool"
        obs, reward, done, info = await env.step({"content": "done"})
        await env.close()
        return reward, done, info

    reward, done, info = run(episode())
    assert done and reward == 1.0 and info["status"] == "completed"


# ---------------------------------------------------------------- CLI
def test_cli_inspection_commands(tmp_path, capsys, workspace):
    data = tmp_path / "data"
    store = EventStore(data / "spool.db")
    rt = Runtime(store, ScriptedModel([call("write_file", path="a.txt", content="x", call_id="c1"), reply("ok")]),
                 data_dir=data)
    sid = rt.start("write a.txt", config=cfg(), workspace=workspace)
    run(rt.run(sid))
    run(rt.aclose())
    store.close()

    base = ["--data-dir", str(data)]
    assert cli_main(base + ["sessions"]) == 0
    assert sid in capsys.readouterr().out
    assert cli_main(base + ["show", sid]) == 0
    assert "← write_file ok" in capsys.readouterr().out
    assert cli_main(base + ["verify", sid]) == 0
    assert "all match" in capsys.readouterr().out
    html_path = tmp_path / "s.html"
    assert cli_main(base + ["view", sid, "-o", str(html_path)]) == 0 and html_path.exists()


def test_cli_approve_flow(tmp_path, capsys, workspace):
    data = tmp_path / "data"
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "k.txt").write_text("v")
    store = EventStore(data / "spool.db")
    model = ScriptedModel([call("web_fetch", url="https://a.test/", call_id="c1"),
                           call("read_file", path="secrets/k.txt", call_id="c2"), reply("done")])
    rt = Runtime(store, model, data_dir=data)
    sid = rt.start("t", config=cfg(tools=["web_fetch", "read_file"], approval="human"), workspace=workspace,
                   fixtures={"web": {"https://a.test/": "page"}})
    assert run(rt.run(sid)).status == "awaiting_approval"
    run(rt.aclose())
    assert cli_main(["--data-dir", str(data), "approve", sid, "c2", "--deny"]) == 0
    assert "denied c2" in capsys.readouterr().out
    rt2 = Runtime(store, model, data_dir=data)
    result = run(rt2.run(sid))
    assert result.final_status == "completed"
    done = [e.data for e in store.load(sid) if e.type == ev.TOOL_COMPLETED and e.data["call_id"] == "c2"][0]
    assert done["kind"] == "rejected"
    run(rt2.aclose())


def test_cli_rules_and_always(tmp_path, capsys, workspace):
    data = tmp_path / "data"
    assert cli_main(["--data-dir", str(data), "rules"]) == 0
    assert "no standing rules" in capsys.readouterr().out
    assert cli_main(["--data-dir", str(data), "rules", "add", "deny", "shop__*", "--note", "no shopping"]) == 0
    added = capsys.readouterr().out
    assert "Never allow shop__*" in added
    rule_id = added.split()[1].rstrip(":")
    assert cli_main(["--data-dir", str(data), "rules", "add", "allow", "mail__send_message", "--arg", "to=*@corp.example"]) == 0
    capsys.readouterr()
    cli_main(["--data-dir", str(data), "rules", "list"])
    listing = capsys.readouterr().out
    assert listing.index("Never allow") < listing.index("Always allow mail__send_message where to is *@corp.example")
    assert cli_main(["--data-dir", str(data), "rules", "rm", rule_id]) == 0
    with pytest.raises(SystemExit):
        cli_main(["--data-dir", str(data), "rules", "rm", rule_id])

    # --always is refused for a Rule-of-Two ask
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "k.txt").write_text("v")
    store = EventStore(data / "spool.db")
    model = ScriptedModel([call("web_fetch", url="https://a.test/", call_id="c1"),
                           call("read_file", path="secrets/k.txt", call_id="c2"), reply("done")])
    rt = Runtime(store, model, data_dir=data)
    sid = rt.start("t", config=cfg(tools=["web_fetch", "read_file"], approval="human"), workspace=workspace,
                   fixtures={"web": {"https://a.test/": "page"}})
    assert run(rt.run(sid)).status == "awaiting_approval"
    run(rt.aclose())
    with pytest.raises(SystemExit, match="Rule of Two"):
        cli_main(["--data-dir", str(data), "approve", sid, "c2", "--always"])


def test_cli_memory(tmp_path, capsys):
    data = tmp_path / "data"
    assert cli_main(["--data-dir", str(data), "memory"]) == 0
    assert "nothing remembered" in capsys.readouterr().out
    cli_main(["--data-dir", str(data), "memory", "add", "Ship orders to the office."])
    mid = capsys.readouterr().out.split()[1]
    cli_main(["--data-dir", str(data), "memory", "list"])
    assert "Ship orders to the office.  (added by you)" in capsys.readouterr().out
    assert cli_main(["--data-dir", str(data), "memory", "forget", mid]) == 0
    with pytest.raises(SystemExit):
        cli_main(["--data-dir", str(data), "memory", "forget", mid])
