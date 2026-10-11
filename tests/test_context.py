from spool import events as ev
from spool.context import build_messages, prefix_breaks
from spool.models.scripted import ScriptedModel, call, calls, reply

from .conftest import cfg, run


def big_file(workspace, name="big.log", lines=2000):
    (workspace / name).write_text("\n".join(f"line {i} " + "x" * 40 for i in range(lines)))


def session(make_runtime, workspace, script, **config):
    model = ScriptedModel(script)
    rt = make_runtime(model)
    sid = rt.start("t", config=cfg(**config), workspace=workspace)
    run(rt.run(sid))
    return rt, sid, model


def test_requests_are_append_only_prefixes(make_runtime, workspace):
    (workspace / "a.txt").write_text("alpha")
    script = [call("read_file", path="a.txt", call_id=f"c{i}") for i in range(4)] + [reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script)
    reqs = model.requests
    for prev, nxt in zip(reqs, reqs[1:]):
        assert nxt.system == prev.system and nxt.tools == prev.tools
        assert nxt.messages[: len(prev.messages)] == prev.messages
    model_events = [e for e in rt.store.load(sid) if e.type == ev.MODEL_RESPONSE]
    assert prefix_breaks(model_events) == 0


def test_unstable_prefix_breaks_every_request(make_runtime, workspace):
    clock = {"t": 1_700_000_000.0}

    def tick():
        clock["t"] += 1.0
        return clock["t"]

    model = ScriptedModel([call("list_files", call_id=f"c{i}") for i in range(3)] + [reply("ok")])
    rt = make_runtime(model, clock=tick)
    sid = rt.start("t", config=cfg(unstable_prefix=True), workspace=workspace)
    run(rt.run(sid))
    assert model.requests[0].system.startswith("Current time:")
    assert len({r.system for r in model.requests}) == len(model.requests)
    model_events = [e for e in rt.store.load(sid) if e.type == ev.MODEL_RESPONSE]
    assert prefix_breaks(model_events) == len(model_events) - 1


def test_large_outputs_are_offloaded_with_a_preview(make_runtime, workspace):
    big_file(workspace)
    script = [call("bash", command="cat big.log", call_id="c1"), reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script, context_strategy="offload",
                             offload_threshold_chars=3000, preview_chars=500, offload_when_over=0.0)
    done = [e.data for e in rt.store.load(sid) if e.type == ev.TOOL_COMPLETED][0]
    assert done["offloaded_path"] == ".spool/outputs/c1.txt"
    assert (workspace / done["offloaded_path"]).read_text() == done["output"]
    tool_msg = [m for m in model.requests[-1].messages if m["role"] == "tool"][0]
    assert len(tool_msg["content"]) < 900 and ".spool/outputs/c1.txt" in tool_msg["content"]


def test_budget_aware_offload_waits_for_context_pressure(make_runtime, workspace):
    (workspace / "page.txt").write_text("p" * 6000)          # ~1.9k tokens per read
    script = [call("bash", command="cat page.txt", call_id=f"c{i}") for i in range(4)] + [reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script, context_strategy="offload",
                             offload_threshold_chars=3000, context_budget_tokens=8000, offload_when_over=0.5)
    offloaded = [bool(e.data.get("offloaded_path")) for e in rt.store.load(sid) if e.type == ev.TOOL_COMPLETED]
    assert offloaded[0] is False and offloaded[-1] is True   # inline while there is room, then offload
    model_events = [e for e in rt.store.load(sid) if e.type == ev.MODEL_RESPONSE]
    assert prefix_breaks(model_events) == 0                 # decided once per output, so still append-only


def test_full_strategy_keeps_output_inline_up_to_the_hard_cap(make_runtime, workspace):
    big_file(workspace)
    script = [call("bash", command="cat big.log", call_id="c1"), reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script, context_strategy="full", hard_cap_chars=20000)
    tool_msg = [m for m in model.requests[-1].messages if m["role"] == "tool"][0]
    assert 19000 < len(tool_msg["content"]) <= 20100 and "chars omitted" in tool_msg["content"]


def test_fold_turns_old_outputs_into_stubs_and_recall_restores_them(make_runtime, workspace):
    for i in range(6):
        (workspace / f"f{i}.txt").write_text(f"content of file {i} " + "y" * 3000)
    script = [call("read_file", path=f"f{i}.txt", call_id=f"c{i}") for i in range(6)]
    # `recall` takes an argument named call_id, so build this call explicitly.
    script += [calls([("recall", {"call_id": "c0"}, "c_recall")]), reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script, context_strategy="fold",
                             context_budget_tokens=4000, compact_trigger=0.5, keep_recent_steps=2,
                             offload_threshold_chars=100000, offload_when_over=0.0)
    events = rt.store.load(sid)
    folds = [e for e in events if e.type == ev.CONTEXT_FOLDED]
    assert folds, "expected at least one fold"
    last_req = model.requests[-1]
    tool_msgs = {m["tool_call_id"]: m["content"] for m in last_req.messages if m["role"] == "tool"}
    assert tool_msgs["c0"].startswith("[folded read_file result")
    assert 'recall with call_id="c0"' in tool_msgs["c0"]
    # recall brought back the original text
    assert "content of file 0" in tool_msgs["c_recall"]
    # folding happens in chunks, not on every step
    assert len(folds) < len([e for e in events if e.type == ev.MODEL_RESPONSE]) - 1


def test_summarize_compaction_replaces_old_turns(make_runtime, workspace):
    for i in range(5):
        (workspace / f"f{i}.txt").write_text(f"file {i} " + "z" * 3000)
    model = ScriptedModel([call("read_file", path=f"f{i}.txt", call_id=f"c{i}") for i in range(5)] + [reply("done")])
    summarizer = ScriptedModel([reply("SUMMARY: read the first files, nothing else")] * 5)
    rt = make_runtime(model, summarizer=summarizer)
    sid = rt.start("read all files", config=cfg(context_strategy="summarize", context_budget_tokens=4000,
                                                compact_trigger=0.5, keep_recent_steps=2,
                                                offload_threshold_chars=100000), workspace=workspace)
    result = run(rt.run(sid))
    assert result.final_status == "completed"
    events = rt.store.load(sid)
    comp = [e for e in events if e.type == ev.CONTEXT_COMPACTED]
    assert comp and all(c.data["summary"].startswith("SUMMARY") for c in comp)
    after = model.requests[-1].messages
    assert after[0] == {"role": "user", "content": "read all files"}
    assert after[1]["content"].startswith("[Summary of earlier progress")
    assert after[2]["role"] == "assistant"
    # the summarizer saw a transcript, not tool schemas
    assert summarizer.requests[0].tools == [] and "TOOL RESULT" in summarizer.requests[0].messages[0]["content"]
    assert rt.state(sid).compactions == len(comp)
    assert rt.state(sid).usage.model_calls == 6 + len(comp)


def test_todo_recitation_is_recorded_in_the_event(make_runtime, workspace):
    (workspace / "a.txt").write_text("a")
    todos = [{"content": "read a", "status": "in_progress"}, {"content": "report", "status": "pending"}]
    script = [call("todo_write", todos=todos, call_id="t1")]
    script += [call("read_file", path="a.txt", call_id=f"r{i}") for i in range(3)] + [reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script, recite_todos=True, recite_every=4)
    done = [e.data for e in rt.store.load(sid) if e.type == ev.TOOL_COMPLETED]
    reminders = [d for d in done if d.get("reminder")]
    assert len(reminders) == 1 and "[~] read a" in reminders[0]["reminder"]
    # and it renders identically on every later request (prefix stays stable)
    model_events = [e for e in rt.store.load(sid) if e.type == ev.MODEL_RESPONSE]
    assert prefix_breaks(model_events) == 0


def test_loop_warning_after_repeated_identical_calls(make_runtime, workspace):
    script = [call("list_files", call_id=f"c{i}") for i in range(4)] + [reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script, loop_threshold=3)
    done = [e.data for e in rt.store.load(sid) if e.type == ev.TOOL_COMPLETED]
    assert [bool(d.get("loop_warning")) for d in done] == [False, False, True, True]
    assert rt.state(sid).loop_warnings == 2


def test_build_messages_upto_seq(make_runtime, workspace):
    script = [call("list_files", call_id="c1"), reply("ok")]
    rt, sid, model = session(make_runtime, workspace, script)
    events = rt.store.load(sid)
    state = rt.state(sid)
    first_resp = next(e.seq for e in events if e.type == ev.MODEL_RESPONSE)
    msgs = build_messages(events, state, upto_seq=first_resp)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
