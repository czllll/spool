"""Connectors: tool policy from MCP annotations, the snapshot in the log, approvals, and exactly-once writes."""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

from spool import events as ev
from spool.connectors import ConnectorError, ConnectorManager, ConnectorSpec, load_connectors, tool_entry
from spool.models.scripted import ByStepModel, ScriptedModel, call, reply
from spool.replay import verify_render
from spool.runtime import Runtime, SimulatedCrash
from spool.state import replay
from spool.tools import PROP_EXTERNAL, PROP_SENSITIVE, PROP_UNTRUSTED

from .conftest import cfg, run

HERE = Path(__file__).resolve().parent
MAIL_SEED = {"inbox": [
    {"id": "m1", "from": "Lee Dental <frontdesk@leedental.example>", "subject": "Appointment confirmed",
     "body": "Your cleaning is confirmed for Tue 20 Oct 2026, 15:30-16:00.", "date": "2026-10-09 09:12"},
]}


# ------------------------------------------------------------- pure policy
def spec(**kw) -> ConnectorSpec:
    return ConnectorSpec(name=kw.pop("name", "mail"), command=("x",), **kw)


def tool(name, annotations=None, props=("to",)):
    schema = {"type": "object", "properties": {p: {"type": "string"} for p in props}, "required": list(props)}
    return {"name": name, "description": f"{name} tool", "inputSchema": schema, "annotations": annotations or {}}


def test_policy_follows_annotations():
    read = tool_entry(spec(sensitive=True), tool("read_message", {"readOnlyHint": True}))
    assert read["properties"] == sorted({PROP_UNTRUSTED, PROP_SENSITIVE}) and read["idempotent"] and not read["confirm"]
    closed = tool_entry(spec(), tool("list_events", {"readOnlyHint": True, "openWorldHint": False}))
    assert closed["properties"] == []
    send = tool_entry(spec(), tool("send_message", {"destructiveHint": False, "openWorldHint": True},
                                   props=("to", "idempotency_key")))
    assert send["name"] == "mail__send_message" and send["properties"] == [PROP_EXTERNAL]
    assert send["confirm"] and send["ledgered"] and send["idem_arg"] == "idempotency_key"
    assert "idempotency_key" not in send["parameters"]["properties"] and send["parameters"]["required"] == ["to"]
    assert send["description"].endswith("The user must approve each call.")
    create = tool_entry(spec(), tool("create_event", {"destructiveHint": False, "openWorldHint": False},
                                     props=("title", "idempotency_key")))
    assert not create["confirm"] and create["ledgered"]  # additive, closed world: no confirmation,
    assert create["properties"] == []                     # and nothing that could leak data
    delete = tool_entry(spec(), tool("delete_event", {"destructiveHint": True, "idempotentHint": True, "openWorldHint": False}))
    assert delete["confirm"] and delete["idempotent"] and not delete["ledgered"] and delete["properties"] == [PROP_EXTERNAL]
    bare = tool_entry(spec(), tool("mystery"))  # no annotations: may write, destroy, reach out
    assert bare["confirm"] and not bare["idempotent"] and not bare["ledgered"] and bare["properties"] == [PROP_EXTERNAL]
    relaxed = tool_entry(spec(tools={"mystery": {"confirm": False, "properties": []}}), tool("mystery"))
    assert not relaxed["confirm"] and relaxed["properties"] == []


def test_connector_file_validation(tmp_path):
    good = tmp_path / "c.toml"
    good.write_text('[connector.mail]\ncommand = ["x"]\nscope = "session"\n[connector.mail.tools.send]\nconfirm = true\n')
    assert load_connectors(good)["mail"].scope == "session"
    for bad in ['[connector.Mail]\ncommand = ["x"]\n', '[connector.mail]\ncommand = []\n',
                '[connector.mail]\ncommand = ["x"]\nscope = "global"\n',
                '[connector.mail]\ncommand = ["x"]\n[connector.mail.tools.send]\nconfrim = true\n']:
        good.write_text(bad)
        with pytest.raises(ConnectorError):
            load_connectors(good)


# ------------------------------------------------------------- end to end
@pytest.fixture
def services(tmp_path):
    seed = tmp_path / "mail.json"
    seed.write_text(json.dumps(MAIL_SEED))
    specs = {
        "mail": ConnectorSpec("mail", ("{python}", "-m", "spool.services.mail", "--db", "{state_dir}/mail.sqlite",
                                       "--seed", str(seed)), scope="session", sensitive=True),
        "calendar": ConnectorSpec("calendar", ("{python}", "-m", "spool.services.calendar", "--db",
                                               "{state_dir}/calendar.sqlite"), scope="session", sensitive=True),
        "echo": ConnectorSpec("echo", (sys.executable, str(HERE / "mcp_echo_server.py")), scope="shared"),
    }
    return specs


def rows(db: Path, table: str) -> list[dict]:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM {table}")]
    finally:
        conn.close()


def test_mail_to_calendar_with_approvals(store, tmp_path, workspace, services):
    script = [
        call("mail__read_message", id="m1", call_id="c1"),
        call("calendar__create_event", title="Dental cleaning", start="2026-10-20T15:30", end="2026-10-20T16:00",
             call_id="c2"),
        call("mail__send_message", to="frontdesk@leedental.example", subject="Re: Appointment confirmed",
             body="Thanks, see you then.", in_reply_to="m1", call_id="c3"),
        reply("Added the appointment and replied."),
    ]
    model = ScriptedModel(script)
    manager = ConnectorManager(services, data_dir=tmp_path / "data")
    rt = Runtime(store, model, data_dir=tmp_path / "data", connectors=manager)
    sid = rt.start("Put my dentist appointment in the calendar and confirm by email.", workspace=workspace,
                   config=cfg(approval="human", tools=["todo_write"], connectors=["mail", "calendar"]))
    first = run(rt.run(sid))
    events = store.load(sid)
    note = next(e.data for e in events if e.type == ev.NOTE and e.data.get("kind") == "connector_tools")
    assert note["errors"] == {} and set(note["servers"]) == {"mail", "calendar"}
    assert note["servers"]["mail"]["era"] == "modern"
    request = model.requests[0]
    assert "Connected services" in request.system and "- mail: The user's mailbox." in request.system
    tool_names = [t["name"] for t in request.tools]
    assert "mail__send_message" in tool_names and "todo_write" in tool_names and "bash" not in tool_names
    send_schema = next(t for t in request.tools if t["name"] == "mail__send_message")
    assert "idempotency_key" not in send_schema["parameters"]["properties"]

    # Reading the email made the session untrusted + sensitive. Adding a calendar entry stays inside the
    # user's own data, so it runs; sending mail both asks every time and would complete the Rule of Two.
    assert first.status == "awaiting_approval" and first.state.awaiting == "c3"
    decisions = {e.data["call_id"]: e.data["rule"] for e in store.load(sid) if e.type == ev.POLICY_DECISION}
    assert decisions == {"c1": "default_allow", "c2": "default_allow", "c3": "rule_of_two"}
    rt.approve(sid, "c3", True)
    done = run(rt.run(sid))
    assert done.final_status == "completed"

    state_dir = rt.ledger_dir(sid)
    [event] = rows(state_dir / "calendar.sqlite", "events")
    assert (event["title"], event["start"], event["end"]) == ("Dental cleaning", "2026-10-20T15:30", "2026-10-20T16:00")
    assert event["idempotency_key"] == f"{sid}:c2"
    sent = [r for r in rows(state_dir / "mail.sqlite", "messages") if r["folder"] == "sent"]
    assert len(sent) == 1 and sent[0]["in_reply_to"] == "m1" and sent[0]["idempotency_key"] == f"{sid}:c3"

    check = verify_render(store.load(sid))  # connector schemas come from the log, no server needed
    assert check.checked == 4 and check.ok
    run(manager.aclose())


def test_crash_after_a_send_does_not_send_twice(store, tmp_path, workspace, services):
    script = [call("mail__send_message", to="a@example.com", subject="s", body="b", call_id="c1"), reply("sent")]
    config = cfg(approval="auto_approve", tools=["todo_write"], connectors=["mail"])

    def crash_after_send(point, info):
        if point == "after_tool_executed" and info.get("tool") == "mail__send_message":
            raise SimulatedCrash()

    data = tmp_path / "data"
    manager = ConnectorManager(services, data_dir=data)
    rt = Runtime(store, ByStepModel(script), data_dir=data, connectors=manager, crash_hook=crash_after_send)
    sid = rt.start("send it", workspace=workspace, config=config)
    with pytest.raises(SimulatedCrash):
        run(rt.run(sid))
    run(manager.aclose())
    # the message went out, but TOOL_COMPLETED was never written
    assert [e.type for e in store.load(sid)][-1] == ev.TOOL_STARTED

    fresh_manager = ConnectorManager(services, data_dir=data)
    resumed = Runtime(store, ByStepModel(script), data_dir=data, connectors=fresh_manager)
    result = run(resumed.run(sid))
    assert result.final_status == "completed"
    completed = next(e.data for e in store.load(sid) if e.type == ev.TOOL_COMPLETED)
    assert completed["kind"] == "deduplicated" and "Already sent" in completed["output"]
    sent = [r for r in rows(resumed.ledger_dir(sid) / "mail.sqlite", "messages") if r["folder"] == "sent"]
    assert len(sent) == 1
    run(fresh_manager.aclose())


def test_crash_in_a_tool_without_a_key_is_reported_not_repeated(store, tmp_path, workspace, services):
    script = [call("echo__add", a=1, b=2, call_id="c1"), reply("done")]
    config = cfg(approval="auto_approve", tools=["todo_write"], connectors=["echo"])

    def crash(point, info):
        if point == "after_tool_started":
            raise SimulatedCrash()

    data = tmp_path / "data"

    async def go():  # one event loop, as in a real process: the shared server is started and closed in it
        manager = ConnectorManager(services, data_dir=data)
        rt = Runtime(store, ByStepModel(script), data_dir=data, connectors=manager, crash_hook=crash)
        sid = rt.start("add", workspace=workspace, config=config)
        with pytest.raises(SimulatedCrash):
            await rt.run(sid)
        await manager.aclose()
        manager = ConnectorManager(services, data_dir=data)
        await Runtime(store, ByStepModel(script), data_dir=data, connectors=manager).run(sid)
        await manager.aclose()
        return sid

    sid = run(go())
    completed = next(e.data for e in store.load(sid) if e.type == ev.TOOL_COMPLETED)
    assert completed["kind"] == "interrupted"  # unannotated, no idempotency key: never silently re-run


def test_unavailable_connector_is_recorded_and_the_session_continues(store, tmp_path, workspace):
    broken = {"ghost": ConnectorSpec("ghost", ("/nonexistent/server",))}
    manager = ConnectorManager(broken, data_dir=tmp_path / "data")
    rt = Runtime(store, ScriptedModel([reply("ok without it")]), data_dir=tmp_path / "data", connectors=manager)
    sid = rt.start("t", workspace=workspace, config=cfg(connectors=["ghost", "missing"], tools=["todo_write"]))
    assert run(rt.run(sid)).final_status == "completed"
    state = replay(store.load(sid))
    note = next(e.data for e in store.load(sid) if e.type == ev.NOTE)
    assert set(note["errors"]) == {"ghost", "missing"} and state.connectors_loaded and state.connector_tools == ()
    run(manager.aclose())
