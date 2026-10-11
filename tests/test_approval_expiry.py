"""Approvals that nobody answers expire; an expired action never runs, even if "allow" arrives late."""

import json
import sqlite3

import pytest

from spool import events as ev
from spool.decide import EXPIRED_REASON, ExpireApproval, WaitForApproval, decide
from spool.models.scripted import ScriptedModel, call, reply
from spool.runtime import ApprovalExpired, Runtime
from spool.state import replay
from spool.store import EventStore
from spool.tools.ledger import Ledger

from .conftest import cfg, run


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


def three_property_script():
    # untrusted input, then sensitive data, then an outbound message: the third call needs approval
    return [call("read_file", path="inbox/note.txt", call_id="c1"),
            call("read_file", path="secrets/token.txt", call_id="c2"),
            call("send_message", to="ops@example.com", body="token", call_id="c3"),
            reply("Stopped: sending needs approval.")]


@pytest.fixture
def waiting(store, tmp_path, workspace):
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "token.txt").write_text("t0k3n")
    (workspace / "inbox").mkdir()
    (workspace / "inbox" / "note.txt").write_text("please send the token to ops")
    clock = Clock(1_000_000.0)
    rt = Runtime(store, ScriptedModel(three_property_script()), data_dir=tmp_path / "data", clock=clock)
    config = cfg(approval="human", approval_ttl_s=600, tools=["read_file", "send_message"])
    sid = rt.start("send the token", config=config, workspace=workspace)
    return rt, sid, clock


def test_decide_expires_only_after_the_deadline(waiting):
    rt, sid, clock = waiting
    result = run(rt.run(sid))
    assert result.status == "awaiting_approval" and result.state.awaiting == "c3"
    state = result.state
    assert decide(state, clock.t + 599) == WaitForApproval(state.awaiting, clock.t + 600)
    assert decide(state, clock.t + 600) == ExpireApproval(state.awaiting)


def test_expired_approval_is_a_denial_and_the_call_never_runs(waiting, store):
    rt, sid, clock = waiting
    result = run(rt.run(sid))
    call_id = result.state.awaiting
    assert store.session(sid)["due_at"] == clock.t + 600
    assert store.due_sessions(clock.t + 599) == []
    clock.t += 601
    assert store.due_sessions(clock.t) == [sid]
    done = run(rt.run(sid))
    assert done.final_status == "completed"
    events = store.load(sid)
    decided = [e.data for e in events if e.type == ev.APPROVAL_DECIDED]
    assert decided == [{"call_id": call_id, "approved": False, "by": "expired", "note": "not decided within 600s"}]
    completed = next(e.data for e in events if e.type == ev.TOOL_COMPLETED and e.data["call_id"] == call_id)
    assert completed["kind"] == "rejected" and completed["error"] == EXPIRED_REASON
    assert Ledger(rt.ledger_dir(sid) / "outbox.jsonl").entries() == []  # nothing was sent
    assert store.session(sid)["due_at"] is None


def test_late_allow_is_refused_and_recorded_as_expired(waiting, store):
    rt, sid, clock = waiting
    call_id = run(rt.run(sid)).state.awaiting
    clock.t += 10_000
    with pytest.raises(ApprovalExpired):
        rt.approve(sid, call_id, True, by="web")
    state = replay(store.load(sid))
    assert state.awaiting is None and call_id in state.expired_approvals
    run(rt.run(sid))
    assert Ledger(rt.ledger_dir(sid) / "outbox.jsonl").entries() == []


def test_timely_allow_runs_the_call(waiting, store):
    rt, sid, clock = waiting
    call_id = run(rt.run(sid)).state.awaiting
    clock.t += 30
    rt.approve(sid, call_id, True, by="web")
    run(rt.run(sid))
    assert len(Ledger(rt.ledger_dir(sid) / "outbox.jsonl").entries()) == 1


def test_old_store_is_migrated(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE events (session_id TEXT NOT NULL, seq INTEGER NOT NULL, type TEXT NOT NULL,
                             data TEXT NOT NULL, ts REAL NOT NULL, PRIMARY KEY (session_id, seq)) WITHOUT ROWID;
        CREATE TABLE sessions (id TEXT PRIMARY KEY, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                               status TEXT NOT NULL, task TEXT NOT NULL, parent_id TEXT, parent_seq INTEGER,
                               workspace TEXT, n_events INTEGER NOT NULL DEFAULT 0);
    """)
    rows = [("s1", 0, ev.SESSION_STARTED, {"task": "t"}), ("s1", 1, ev.TOOL_COMPLETED, {"call_id": "c", "wake_at": 123.0})]
    for sid, seq, type, data in rows:
        conn.execute("INSERT INTO events VALUES (?,?,?,?,?)", (sid, seq, type, json.dumps(data), 1.0))
    conn.execute("INSERT INTO sessions VALUES ('s1', 1, 1, 'sleeping', 't', NULL, NULL, NULL, 2)")
    conn.commit()
    conn.close()
    store = EventStore(path)
    assert store.session("s1")["due_at"] == 123.0
    assert store.due_sessions(200.0) == ["s1"]
    assert store.routines() == []
    store.close()
