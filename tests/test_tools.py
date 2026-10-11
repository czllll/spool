import json
import multiprocessing
import os
from pathlib import Path

import pytest

from spool.config import AgentConfig
from spool.tools import PathError, ToolContext, classify_path, resolve_path, validate_args
from spool.tools.builtin import builtin_registry
from spool.tools.edit import EditError, apply_edit
from spool.tools.ledger import Ledger

from .conftest import run


def ctx(workspace: Path, tmp_path: Path, **config) -> ToolContext:
    from spool.sandbox.local import LocalSandbox

    return ToolContext(
        workspace=workspace, sandbox=LocalSandbox(workspace), session_id="s", call_id="c1", idem_key="s:c1",
        config=AgentConfig(**config), ledger_dir=tmp_path / "ledger", fixtures={"web": {"https://a.test/": "page"}},
        history=lambda cid: {"output": "original", "ok": True} if cid == "old" else None, now=lambda: 1000.0,
    )


def tool(name):
    return builtin_registry().get(name).handler


# ------------------------------------------------------------------ edit
def test_edit_exact_unique_and_replace_all():
    assert apply_edit("a = 1\nb = 2\n", "b = 2", "b = 3")[0] == "a = 1\nb = 3\n"
    with pytest.raises(EditError, match="occurs 2 times"):
        apply_edit("x\nx\n", "x", "y")
    assert apply_edit("x\nx\n", "x", "y", replace_all=True)[0] == "y\ny\n"


def test_edit_is_idempotent():
    text, msg = apply_edit("value = 2\n", "value = 1", "value = 2")
    assert text == "value = 2\n" and "already applied" in msg


def test_edit_fuzzy_indentation():
    src = "def f():\n    if x:\n        return 1\n    return 2\n"
    new, msg = apply_edit(src, "if x:\n    return 1", "if x:\n        return 10")
    assert "return 10" in new and "matched ignoring" in msg


def test_edit_fuzzy_reindents_the_replacement():
    # The failure seen in the first real eval trial: one separator space copied into both strings.
    fixed = apply_edit("[server]\nport = 8000\n", " port = 8000", " port = 8080")[0]
    assert fixed == "[server]\nport = 8080\n"
    # Re-running the same edit after a crash is a no-op, not an error.
    assert apply_edit(fixed, " port = 8000", " port = 8080")[0] == fixed
    # Dropped common indentation on a multi-line block.
    src = "def f():\n    a = 1\n    b = 2\n"
    assert apply_edit(src, "a = 1\nb = 2", "a = 10\nb = 20")[0] == "def f():\n    a = 10\n    b = 20\n"


def test_edit_not_found_gives_hint():
    with pytest.raises(EditError, match="Closest existing line"):
        apply_edit("def compute_total(items):\n    pass\n", "def compute_totals(items):", "def g():")


# -------------------------------------------------------------- paths
def test_path_jail(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (tmp_path / "outside.txt").write_text("x")
    os.symlink(tmp_path / "outside.txt", ws / "link.txt")
    assert resolve_path(ws, "a/b.txt") == Path(os.path.realpath(ws)) / "a" / "b.txt"
    for bad in ["../outside.txt", str(tmp_path / "outside.txt"), "link.txt", "a/../../outside.txt", ""]:
        with pytest.raises(PathError):
            resolve_path(ws, bad)


def test_classify_path(tmp_path):
    cfg = AgentConfig()
    assert classify_path(tmp_path, "secrets/key", cfg) == {"sensitive_data"}
    assert classify_path(tmp_path, "./downloads/x.md", cfg) == {"untrusted_input"}
    assert classify_path(tmp_path, "src/secrets/key", cfg) == frozenset()
    assert classify_path(tmp_path, "../etc/passwd", cfg) == frozenset()


def test_validate_args():
    schema = builtin_registry().get("todo_write").parameters
    assert validate_args(schema, {"todos": [{"content": "a", "status": "pending"}]}) == []
    errors = validate_args(schema, {"todos": [{"content": "a", "status": "done"}], "extra": 1})
    assert any("must be one of" in e for e in errors) and any("unexpected argument 'extra'" in e for e in errors)
    assert validate_args({"type": "object", "properties": {"n": {"type": "integer"}}}, {"n": True})


# -------------------------------------------------------------- handlers
def test_write_read_edit_roundtrip(tmp_path, workspace):
    c = ctx(workspace, tmp_path)
    assert run(tool("write_file")(c, {"path": "src/app.py", "content": "x = 1\ny = 2\n"})).ok
    out = run(tool("read_file")(c, {"path": "src/app.py"})).output
    assert "1\tx = 1" in out and "[lines 1-2 of 2]" in out
    assert run(tool("edit_file")(c, {"path": "src/app.py", "old_string": "y = 2", "new_string": "y = 3"})).ok
    assert (workspace / "src/app.py").read_text() == "x = 1\ny = 3\n"


def test_read_file_pages(tmp_path, workspace):
    (workspace / "long.txt").write_text("\n".join(str(i) for i in range(1, 1001)))
    c = ctx(workspace, tmp_path)
    out = run(tool("read_file")(c, {"path": "long.txt", "offset": 990, "limit": 5})).output
    assert "990\t990" in out and "[lines 990-994 of 1000; call read_file with offset=995" in out


def test_protected_and_internal_dirs_are_read_only(tmp_path, workspace):
    c = ctx(workspace, tmp_path)
    for path in ["secrets/new.txt", ".spool/x.txt"]:
        result = run(tool("write_file")(c, {"path": path, "content": "x"}))
        assert not result.ok and ("read-only" in result.error or "reserved" in result.error)


def test_list_files_hides_protected_contents(tmp_path, workspace):
    (workspace / "secrets").mkdir()
    (workspace / "secrets" / "key.txt").write_text("k")
    (workspace / "src").mkdir()
    (workspace / "src" / "a.py").write_text("print(1)")
    out = run(tool("list_files")(ctx(workspace, tmp_path), {})).output
    assert "src/a.py" in out and "secrets/  (protected" in out and "key.txt" not in out


def test_todo_and_recall(tmp_path, workspace):
    c = ctx(workspace, tmp_path)
    res = run(tool("todo_write")(c, {"todos": [{"content": "a", "status": "completed"},
                                                {"content": "b", "status": "in_progress"}]}))
    assert res.data["todos"][1] == {"content": "b", "status": "in_progress"} and "[x] a" in res.output
    assert run(tool("recall")(c, {"call_id": "old"})).output == "original"
    assert not run(tool("recall")(c, {"call_id": "nope"})).ok


def test_web_fetch_uses_fixtures_and_logs_requests(tmp_path, workspace):
    c = ctx(workspace, tmp_path)
    assert run(tool("web_fetch")(c, {"url": "https://a.test/"})).output == "page"
    assert not run(tool("web_fetch")(c, {"url": "https://b.test/?leak=1"})).ok
    urls = [e["url"] for e in Ledger(tmp_path / "ledger" / "net.jsonl").entries()]
    assert urls == ["https://a.test/"]  # same idem key: the second request is the same call id


def test_send_message_is_deduplicated_by_key(tmp_path, workspace):
    c = ctx(workspace, tmp_path)
    first = run(tool("send_message")(c, {"to": "a@x", "body": "hi"}))
    second = run(tool("send_message")(c, {"to": "a@x", "body": "hi"}))
    assert first.data["meta"]["deduplicated"] is False and second.data["meta"]["deduplicated"] is True
    assert len(Ledger(tmp_path / "ledger" / "outbox.jsonl").entries()) == 1


def _record(path, key, i):
    Ledger(path).record(key, {"i": i})


def test_ledger_is_safe_across_processes(tmp_path):
    path = tmp_path / "outbox.jsonl"
    procs = [multiprocessing.get_context("spawn").Process(target=_record, args=(path, "same-key", i)) for i in range(8)]
    for p in procs:
        p.start()
    for p in procs:
        p.join()
    assert len(Ledger(path).entries()) == 1


def test_sleep_and_spawn_unavailable(tmp_path, workspace):
    c = ctx(workspace, tmp_path)
    res = run(tool("sleep")(c, {"seconds": 30, "reason": "wait for CI"}))
    assert res.data["wake_at"] == 1030.0 and "wait for CI" in res.output
    assert not run(tool("spawn_subagent")(c, {"task": "x"})).ok
