"""Crash at every interesting point, resume from the log, check exactly-once effects."""

import json
import os
import subprocess
import sys
import textwrap

import pytest

from spool import events as ev
from spool.models.scripted import ByStepModel, call, reply
from spool.runtime import Runtime, SimulatedCrash
from spool.tools.ledger import Ledger

from .conftest import cfg, run

SCRIPT = [
    call("write_file", path="report.txt", content="total=42", call_id="c1"),
    call("send_message", to="team@example.com", subject="report", body="total=42", call_id="c2"),
    call("bash", command="echo appended >> log.txt", call_id="c3"),
    reply("sent the report"),
]


def crash_at(point, tool=None):
    state = {"fired": False}

    def hook(at, info):
        if state["fired"] or at != point or (tool and info.get("tool") != tool):
            return
        state["fired"] = True
        raise SimulatedCrash(f"{point}:{tool}")

    return hook


@pytest.mark.parametrize("point,tool", [
    ("before_model_call", None),
    ("after_model_response", None),
    ("after_tool_started", "send_message"),
    ("after_tool_executed", "send_message"),   # the dangerous window: effect done, result not logged
    ("after_tool_started", "write_file"),
    ("after_tool_executed", "write_file"),
])
def test_resume_after_crash_sends_exactly_once(tmp_path, store, workspace, point, tool):
    data_dir = tmp_path / "data"
    rt1 = Runtime(store, ByStepModel(SCRIPT), data_dir=data_dir, crash_hook=crash_at(point, tool))
    sid = rt1.start("send the report", config=cfg(tools=["write_file", "send_message", "bash"]), workspace=workspace)
    with pytest.raises(SimulatedCrash):
        run(rt1.run(sid))

    # A brand-new runtime (as after a process restart) resumes from the store alone.
    rt2 = Runtime(store, ByStepModel(SCRIPT), data_dir=data_dir, crash_hook=None)
    result = run(rt2.run(sid))
    assert result.final_status == "completed"
    outbox = Ledger(data_dir / "ledgers" / sid / "outbox.jsonl").entries()
    assert len(outbox) == 1 and outbox[0]["body"] == "total=42"
    assert (workspace / "report.txt").read_text() == "total=42"
    assert (workspace / "log.txt").read_text() == "appended\n"
    run(rt2.aclose())


def test_crash_during_bash_is_reported_not_rerun(tmp_path, store, workspace):
    data_dir = tmp_path / "data"
    rt1 = Runtime(store, ByStepModel(SCRIPT), data_dir=data_dir, crash_hook=crash_at("after_tool_executed", "bash"))
    sid = rt1.start("t", config=cfg(tools=["write_file", "send_message", "bash"]), workspace=workspace)
    with pytest.raises(SimulatedCrash):
        run(rt1.run(sid))
    rt2 = Runtime(store, ByStepModel(SCRIPT), data_dir=data_dir)
    result = run(rt2.run(sid))
    assert result.final_status == "completed"
    bash_done = [e.data for e in store.load(sid) if e.type == ev.TOOL_COMPLETED and e.data["name"] == "bash"][0]
    assert bash_done["kind"] == "interrupted" and "may or may not have taken effect" in bash_done["error"]
    # The command ran once before the crash and was not repeated.
    assert (workspace / "log.txt").read_text() == "appended\n"
    run(rt2.aclose())


def test_real_sigkill_and_resume_in_a_new_process(tmp_path, workspace):
    """kill -9 the agent process right after the side effect, resume in another process."""
    db = tmp_path / "spool.db"
    data_dir = tmp_path / "data"
    script_path = tmp_path / "agent.py"
    script_path.write_text(textwrap.dedent(f"""
        import asyncio, json, sys
        from spool.store import EventStore
        from spool.runtime import Runtime
        from spool.config import AgentConfig
        from spool.models.scripted import ByStepModel, call, reply
        SCRIPT = [
            call("send_message", to="team@example.com", body="hello", call_id="c1"),
            reply("done"),
        ]
        store = EventStore({str(db)!r})
        rt = Runtime(store, ByStepModel(SCRIPT), data_dir={str(data_dir)!r})
        async def main():
            if sys.argv[1] == "start":
                sid = rt.start("send hello", workspace={str(workspace)!r},
                               config=AgentConfig(sandbox="process", tools=("send_message",)))
                print(sid, flush=True)
            else:
                sid = sys.argv[2]
            result = await rt.run(sid)
            print(json.dumps({{"status": result.final_status}}), flush=True)
        asyncio.run(main())
    """))
    env = dict(os.environ, SPOOL_CRASH_AT="after_tool_executed:send_message:1")
    first = subprocess.run([sys.executable, str(script_path), "start"], capture_output=True, text=True, env=env)
    assert first.returncode == -9, first.stderr
    sid = first.stdout.split()[0]

    env.pop("SPOOL_CRASH_AT")
    second = subprocess.run([sys.executable, str(script_path), "resume", sid], capture_output=True, text=True, env=env)
    assert second.returncode == 0, second.stderr
    assert json.loads(second.stdout.strip().splitlines()[-1]) == {"status": "completed"}
    outbox = Ledger(data_dir / "ledgers" / sid / "outbox.jsonl").entries()
    assert len(outbox) == 1
