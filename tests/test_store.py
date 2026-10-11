import os
import subprocess
import sys
import textwrap

import pytest

from spool import events as ev
from spool.store import ConcurrencyError, EventStore


def test_append_and_load_roundtrip(store):
    store.append("s1", ev.SESSION_STARTED, {"task": "t", "workspace": "/w"}, expected_seq=0)
    store.append("s1", ev.USER_MESSAGE, {"text": "hello"}, expected_seq=1)
    events = store.load("s1")
    assert [e.seq for e in events] == [0, 1]
    assert events[1].data == {"text": "hello"}
    assert store.load("s1", upto_seq=0)[0].type == ev.SESSION_STARTED


def test_seq_conflict_is_rejected(store):
    store.append("s1", ev.SESSION_STARTED, {"task": "t"}, expected_seq=0)
    store.append("s1", ev.USER_MESSAGE, {"text": "a"}, expected_seq=1)
    with pytest.raises(ConcurrencyError):
        store.append("s1", ev.USER_MESSAGE, {"text": "b"}, expected_seq=1)
    assert [e.data["text"] for e in store.load("s1")[1:]] == ["a"]


def test_unknown_event_type_rejected(store):
    with pytest.raises(ValueError):
        store.append("s1", "made_up", {}, expected_seq=0)


def test_sessions_projection_tracks_status(store):
    store.append("s1", ev.SESSION_STARTED, {"task": "do it", "workspace": "/w"}, expected_seq=0)
    assert store.session("s1")["status"] == "running"
    store.append("s1", ev.APPROVAL_REQUESTED, {"call_id": "c1"}, expected_seq=1)
    assert store.session("s1")["status"] == "awaiting_approval"
    store.append("s1", ev.APPROVAL_DECIDED, {"call_id": "c1", "approved": True}, expected_seq=2)
    assert store.session("s1")["status"] == "running"
    store.append("s1", ev.SESSION_FINISHED, {"status": "completed"}, expected_seq=3)
    info = store.session("s1")
    assert info["status"] == "completed" and info["n_events"] == 4 and info["task"] == "do it"


def test_data_is_canonical_json(store):
    e = store.append("s1", ev.SESSION_STARTED, {"b": 1, "a": {"y": 2, "x": 1}}, expected_seq=0)
    assert list(e.data) == ["a", "b"]


def test_events_survive_kill_9(tmp_path):
    db = tmp_path / "crash.db"
    script = textwrap.dedent(f"""
        import os, signal
        from spool.store import EventStore
        s = EventStore({str(db)!r})
        s.append("s1", "session_started", {{"task": "t"}}, expected_seq=0)
        s.append("s1", "user_message", {{"text": "durable"}}, expected_seq=1)
        os.kill(os.getpid(), signal.SIGKILL)
    """)
    proc = subprocess.run([sys.executable, "-c", script], capture_output=True)
    assert proc.returncode == -9
    reopened = EventStore(db)
    assert [e.data.get("text") for e in reopened.load("s1")] == [None, "durable"]
    reopened.close()
