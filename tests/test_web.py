"""The web app: sessions run in the background, the UI follows the log over SSE."""

import json
import time

import pytest

pytest.importorskip("starlette")
from starlette.testclient import TestClient  # noqa: E402

from spool.models.scripted import ScriptedModel, call, reply  # noqa: E402
from spool.runtime import Runtime  # noqa: E402
from spool.store import EventStore  # noqa: E402
from spool.web.app import create_app  # noqa: E402

from .conftest import cfg  # noqa: E402


def wait_for(client, sid, predicate, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get(f"/api/sessions/{sid}").json()
        if predicate(s):
            return s
        time.sleep(0.05)
    raise AssertionError(f"timed out; last summary: {s}")


def sse_events(text):
    events, current = [], {}
    for line in text.splitlines():
        if not line:
            if current:
                events.append(current)
            current = {}
        elif line.startswith(":"):
            continue
        else:
            key, _, value = line.partition(": ")
            current[key] = value
    return events


@pytest.fixture
def app_and_client(tmp_path):
    store = EventStore(tmp_path / "db.sqlite")

    def make(script, **config):
        rt = Runtime(store, ScriptedModel(script), data_dir=tmp_path / "data")
        app = create_app(rt, default_config=cfg(**config))
        return TestClient(app)

    yield make
    store.close()


def test_page_and_static_assets(app_and_client):
    with app_and_client([]) as client:
        assert "Spool" in client.get("/").text
        assert client.get("/static/app.js").status_code == 200
        assert "--paper" in client.get("/static/app.css").text


def test_task_with_approval_end_to_end(app_and_client, tmp_path):
    ws = tmp_path / "ws"
    (ws / "secrets").mkdir(parents=True)
    (ws / "secrets" / "token.txt").write_text("t0k3n")
    script = [call("web_fetch", url="https://docs.example/a", call_id="c1"),
              call("read_file", path="secrets/token.txt", call_id="c2"),
              reply("Read the token after approval.")]
    with app_and_client(script, tools=["web_fetch", "read_file"]) as client:
        created = client.post("/api/sessions", json={"task": "check the docs", "approval": "human", "workspace": str(ws)})
        assert created.status_code == 201
        sid = created.json()["id"]
        waiting = wait_for(client, sid, lambda s: s["awaiting"] == "c2" and not s["running"])
        assert set(waiting["taint"]) == {"untrusted_input", "external_comm"}
        assert sid in [row["id"] for row in client.get("/api/sessions").json()]

        assert client.post(f"/api/sessions/{sid}/approvals", json={"call_id": "c2", "approved": True}).status_code == 200
        done = wait_for(client, sid, lambda s: s["status"] == "finished" and not s["running"])
        assert done["final"]["status"] == "completed" and done["step"] == 3

        replayed = sse_events(client.get(f"/api/sessions/{sid}/events?follow=0").text)
        data_events = [e for e in replayed if e.get("event") == "event"]
        types = [json.loads(e["data"])["type"] for e in data_events]
        assert types[0] == "session_started" and types[-1] == "session_finished"
        assert "approval_requested" in types and "approval_decided" in types
        assert [int(e["id"]) for e in data_events] == list(range(len(data_events)))  # id = seq
        assert replayed[-1]["event"] == "status"

        # A reconnect with Last-Event-ID gets only what it has not seen.
        later = sse_events(client.get(f"/api/sessions/{sid}/events?follow=0", headers={"Last-Event-ID": "5"}).text)
        assert [int(e["id"]) for e in later if e.get("event") == "event"][0] == 6


def test_follow_up_and_branch(app_and_client, tmp_path):
    script = [reply("first answer"), reply("second answer"), reply("branch answer")]
    with app_and_client(script) as client:
        sid = client.post("/api/sessions", json={"task": "q1", "workspace": str(tmp_path / "w")}).json()["id"]
        wait_for(client, sid, lambda s: s["status"] == "finished" and not s["running"])
        assert client.post(f"/api/sessions/{sid}/messages", json={"text": "q2"}).status_code == 200
        done = wait_for(client, sid, lambda s: s["status"] == "finished" and s["step"] == 2 and not s["running"])
        assert done["final"]["answer"] == "second answer"
        branched = client.post(f"/api/sessions/{sid}/fork", json={"at_seq": 1}).json()
        result = wait_for(client, branched["id"], lambda s: s["status"] == "finished" and not s["running"])
        assert result["final"]["answer"] == "branch answer" and result["parent"]["kind"] == "fork"


def test_files_are_jailed_and_protected(app_and_client, tmp_path):
    ws = tmp_path / "ws"
    (ws / "secrets").mkdir(parents=True)
    (ws / "secrets" / "k.txt").write_text("hidden")
    (ws / "notes.md").write_text("# hello\n")
    with app_and_client([reply("ok")]) as client:
        sid = client.post("/api/sessions", json={"task": "t", "workspace": str(ws)}).json()["id"]
        listing = client.get(f"/api/sessions/{sid}/files?path=.").json()
        assert {"notes.md", "secrets"} <= {e["name"] for e in listing["entries"]}
        assert client.get(f"/api/sessions/{sid}/files?path=notes.md").json()["content"] == "# hello\n"
        assert client.get(f"/api/sessions/{sid}/files?path=../").status_code == 400
        assert client.get(f"/api/sessions/{sid}/files?path=secrets/k.txt").status_code == 403
        assert client.get("/api/sessions/nope").status_code == 404
        assert client.post("/api/sessions", json={"task": "  "}).status_code == 400


def test_stop_then_resume(app_and_client, tmp_path):
    with app_and_client([reply("done")]) as client:
        sid = client.post("/api/sessions", json={"task": "t", "workspace": str(tmp_path / "w")}).json()["id"]
        wait_for(client, sid, lambda s: not s["running"])
        assert client.post(f"/api/sessions/{sid}/stop", json={}).json()["stopped"] is False
        assert client.post(f"/api/sessions/{sid}/resume", json={}).status_code == 200


def test_read_only_server_refuses_writes(tmp_path):
    store = EventStore(tmp_path / "db.sqlite")
    rt = Runtime(store, ScriptedModel([]), data_dir=tmp_path / "data")
    sid = rt.start("recorded task", config=cfg(), workspace=tmp_path / "w")
    with TestClient(create_app(rt, default_config=cfg(), read_only=True)) as client:
        assert client.get("/api/config").json()["read_only"] is True
        assert client.post("/api/sessions", json={"task": "x"}).status_code == 403
        for path in ("messages", "approvals", "resume", "stop", "fork"):
            assert client.post(f"/api/sessions/{sid}/{path}", json={}).status_code == 403
        assert client.get(f"/api/sessions/{sid}").status_code == 200
    store.close()
