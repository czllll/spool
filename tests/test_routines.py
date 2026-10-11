"""Routines: exactly-once firing, misfire and overlap policies, file triggers, webhooks, crash repair."""

from datetime import datetime, timezone

import pytest

from spool import events as ev
from spool.models.scripted import call, reply
from spool.routines import (
    Fire,
    RoutineError,
    RoutineSpec,
    RoutineState,
    Scheduler,
    Trigger,
    due,
    render_task,
    replay_routine,
    specs_from_toml,
    stream_id,
)
from spool.runtime import Runtime, SimulatedCrash

from .conftest import cfg, run


def ts(*args) -> float:
    return datetime(*args, tzinfo=timezone.utc).timestamp()


class Clock:
    def __init__(self, t: float) -> None:
        self.t = t

    def __call__(self) -> float:
        return self.t


class Echo:
    """Answers every request with the task it was given (so tests can read the rendered task)."""

    name = "echo"

    async def complete(self, request):
        return reply("done: " + request.messages[0]["content"])

    async def aclose(self):
        return None


def daily(name="digest", **kw) -> RoutineSpec:
    kw.setdefault("triggers", (Trigger("cron", cron="0 9 * * *"),))
    kw.setdefault("timezone", "UTC")
    kw.setdefault("config", {"model": "scripted", "sandbox": "process", "temperature": None})
    return RoutineSpec(name=name, task="Summarize the inbox for {date}.", **kw)


@pytest.fixture
def setup(store, tmp_path):
    clock = Clock(ts(2026, 10, 11, 8, 0))

    def make(model=None):
        rt = Runtime(store, model or Echo(), data_dir=tmp_path / "data", clock=clock)
        return rt, Scheduler(rt, base_config=cfg(), clock=clock)

    return clock, make


# ------------------------------------------------------------------ specs
@pytest.mark.parametrize("bad", [
    dict(name="Bad Name"),
    dict(triggers=(Trigger("cron", cron="0 25 * * *"),)),
    dict(triggers=(Trigger("interval", every_s=10),)),
    dict(triggers=(Trigger("file", path="/etc"),)),
    dict(triggers=(Trigger("file", path="../up"),)),
    dict(overlap="queue"),
    dict(timezone="Mars/Olympus"),
    dict(config={"approval": "maybe"}),
])
def test_invalid_specs_are_rejected(bad):
    spec = daily(**{k: v for k, v in bad.items() if k != "name"}) if "name" not in bad else daily(name=bad["name"])
    with pytest.raises(RoutineError):
        spec.validate()


def test_toml_shorthands():
    specs = specs_from_toml({"routine": [{"name": "inbox", "task": "t", "cron": "0 9 * * 1-5", "watch": "drop",
                                          "webhook": True, "every": 3600}]})
    kinds = [t.kind for t in specs[0].triggers]
    assert kinds == ["cron", "interval", "file", "webhook"]


def test_render_task_only_replaces_known_placeholders():
    assert render_task("on {date}: {files} {unknown} {{x}}", {"date": "2026-10-11", "files": "a.csv"}) == \
        "on 2026-10-11: a.csv {unknown} {{x}}"


# --------------------------------------------------------------- pure due
def saved(spec: RoutineSpec, armed_at: float) -> RoutineState:
    return replay_routine([ev.Event(stream_id(spec.name), 0, ev.ROUTINE_SAVED, {"spec": spec.to_dict(), "armed_at": armed_at})])


def test_due_fires_the_latest_missed_time_once():
    state = saved(daily(), ts(2026, 10, 8, 12, 0))
    fires = due(state, ts(2026, 10, 11, 9, 30))  # down since Thursday noon: Fri, Sat, Sun 09:00 missed
    assert len(fires) == 1
    assert fires[0].scheduled_for == ts(2026, 10, 11, 9, 0) and fires[0].missed == 2 and fires[0].skip_reason is None
    skipping = saved(daily(misfire="skip"), ts(2026, 10, 8, 12, 0))
    assert due(skipping, ts(2026, 10, 11, 9, 30))[0].skip_reason  # 30 min late: recorded as missed, not run
    assert due(skipping, ts(2026, 10, 11, 9, 1))[0].skip_reason is None  # within the grace period: runs
    assert due(state, ts(2026, 10, 8, 13, 0)) == []


def test_interval_and_paused():
    spec = daily(triggers=(Trigger("interval", every_s=3600),))
    state = saved(spec, ts(2026, 10, 11, 8, 0))
    assert due(state, ts(2026, 10, 11, 8, 59)) == []
    assert due(state, ts(2026, 10, 11, 9, 0))[0].scheduled_for == ts(2026, 10, 11, 9, 0)
    paused = replay_routine([
        ev.Event(stream_id("digest"), 0, ev.ROUTINE_SAVED, {"spec": spec.to_dict(), "armed_at": 0.0}),
        ev.Event(stream_id("digest"), 1, ev.ROUTINE_PAUSED, {}),
    ])
    assert due(paused, ts(2026, 10, 11, 12, 0)) == []


# --------------------------------------------------------------- scheduler
def test_each_scheduled_time_fires_exactly_once(setup, store):
    clock, make = setup
    rt, sched = make()
    sched.save(daily())
    assert sched.tick() == []
    clock.t = ts(2026, 10, 11, 9, 0, 5)
    results = sched.tick()
    assert [(r.session_id, r.started) for r in results] == [("r-digest-0001", True)]
    assert sched.tick() == []  # same minute again: nothing new
    _, other = make()           # a second scheduler on the same store sees it as handled too
    assert other.tick() == []
    started = store.load("r-digest-0001")[0].data
    assert started["task"] == "Summarize the inbox for 2026-10-11."
    assert started["parent"] == {"kind": "routine", "routine": "digest", "fire_key": f"cron0:{int(ts(2026, 10, 11, 9, 0))}"}
    run(rt.run("r-digest-0001"))
    clock.t = ts(2026, 10, 12, 9, 0, 30)
    assert [r.session_id for r in sched.tick()] == ["r-digest-0002"]


def test_racing_schedulers_fire_once(setup, store):
    clock, make = setup
    _, a = make()
    _, b = make()
    a.save(daily())
    clock.t = ts(2026, 10, 11, 9, 1)
    fire = due(a.state("digest"), clock.t)[0]
    b.state("digest")  # b has read the routine before a fires
    first = a._perform("digest", fire)
    second = b._perform("digest", fire)  # b's append hits a's seq, b re-reads and sees it handled
    assert first.started and second is None
    fired = [e for e in store.load(stream_id("digest")) if e.type == ev.ROUTINE_FIRED]
    assert len(fired) == 1


def test_overlap_skip_records_the_skip(setup, store, tmp_path):
    clock, make = setup
    script = [call("web_fetch", url="https://example.com/a", call_id="c1"),
              call("read_file", path="secrets/key.txt", call_id="c2")]
    from spool.models.scripted import ScriptedModel

    rt, sched = make(ScriptedModel(script))
    spec = daily(config={"model": "scripted", "sandbox": "process", "temperature": None, "approval": "human",
                         "tools": ["web_fetch", "read_file"]})
    sched.save(spec)
    ws = sched.workspace(spec)
    (ws / "secrets").mkdir(parents=True)
    (ws / "secrets" / "key.txt").write_text("k")
    clock.t = ts(2026, 10, 11, 9, 0, 10)
    sched.tick()
    result = run(rt.run("r-digest-0001"))
    assert result.status == "awaiting_approval"
    assert result.state.config.approval_ttl_s == 600  # nobody is watching a routine run
    clock.t = ts(2026, 10, 11, 9, 5)  # approval still pending: the run is "busy"
    assert result.due_at == ts(2026, 10, 11, 9, 0, 10) + 600
    clock.t = ts(2026, 10, 12, 9, 0, 1)
    skipped = sched.tick()
    assert skipped[0].skipped and "has not finished" in skipped[0].skipped
    types = [e.type for e in store.load(stream_id("digest"))]
    assert types.count(ev.ROUTINE_SKIPPED) == 1


def test_fire_recorded_but_start_lost_is_repaired(setup, store, monkeypatch):
    clock, make = setup
    _, sched = make()
    sched.save(daily())
    clock.t = ts(2026, 10, 11, 9, 0, 1)

    def crash(*a, **k):
        raise SimulatedCrash()

    monkeypatch.setattr(sched, "_start", crash)
    with pytest.raises(SimulatedCrash):
        sched.tick()
    assert not store.exists("r-digest-0001")  # ROUTINE_FIRED is in the log, the session is not
    _, fresh = make()                          # "restarted" scheduler
    repaired = fresh.tick()
    assert [(r.session_id, r.started) for r in repaired] == [("r-digest-0001", True)]
    assert fresh.tick() == []                  # started once, never twice
    assert store.routine("digest")["fires"] == 1


def test_webhook_delivery_key_deduplicates(setup, store):
    clock, make = setup
    rt, sched = make()
    sched.save(daily(triggers=(Trigger("webhook"),)))
    first = sched.fire("digest", payload={"order": 7}, key="evt-1")
    again = sched.fire("digest", payload={"order": 7}, key="evt-1")
    assert first.started and again.deduplicated and again.session_id == first.session_id
    busy = sched.fire("digest", payload={"order": 8}, key="evt-2")  # run 1 has not finished yet
    assert busy.skipped and busy.session_id is None
    assert not any(e.type == ev.ROUTINE_SKIPPED for e in store.load(stream_id("digest")))  # nothing recorded
    run(rt.run(first.session_id))
    retried = sched.fire("digest", payload={"order": 8}, key="evt-2")  # the sender's retry goes through
    assert retried.started and retried.session_id == "r-digest-0002"
    assert "{\"order\": 8}" not in store.load(retried.session_id)[0].data["task"]  # task has no {payload}
    no_hook = daily(name="plain")
    sched.save(no_hook)
    with pytest.raises(RoutineError, match="no webhook"):
        sched.fire("plain")
    manual = sched.fire("plain", kind="manual")  # "Run now" works without a webhook trigger, even paused
    assert manual.started


def test_file_trigger_fires_for_new_files_only(setup, store):
    clock, make = setup
    rt, sched = make()
    spec = daily(triggers=(Trigger("file", path="inbox", pattern="*.csv"),))
    spec = RoutineSpec(**{**spec.__dict__, "task": "Process:\n{files}"})
    sched.save(spec)
    inbox = sched.workspace(spec) / "inbox"
    inbox.mkdir(parents=True)
    assert sched.tick() == []
    (inbox / "a.csv").write_text("1")
    (inbox / "notes.txt").write_text("x")  # not matching the pattern
    first = sched.tick()
    assert [r.session_id for r in first] == ["r-digest-0001"]
    assert store.load("r-digest-0001")[0].data["task"] == "Process:\ninbox/a.csv"
    run(rt.run("r-digest-0001"))
    (inbox / "a.csv").write_text("changed")  # edited in place (e.g. by the run itself): no new fire
    assert sched.tick() == []
    (inbox / "b.csv").write_text("2")
    assert store.load(sched.tick()[0].session_id)[0].data["task"] == "Process:\ninbox/b.csv"


def test_pause_resume_and_delete(setup, store):
    clock, make = setup
    _, sched = make()
    sched.save(daily())
    sched.pause("digest")
    clock.t = ts(2026, 10, 11, 9, 0, 1)
    assert sched.tick() == []
    clock.t = ts(2026, 10, 11, 10, 0)
    sched.resume("digest")  # re-armed from now: 09:00 today is not caught up
    assert sched.tick() == []
    assert sched.next_times(sched.state("digest"), clock.t) == [ts(2026, 10, 12, 9, 0)]
    sched.delete("digest")
    assert store.routines() == []
    with pytest.raises(KeyError):
        sched.pause("digest")
    sched.save(daily())  # re-created: the stream continues, ids stay unique
    assert store.routine("digest")["deleted"] is False


def test_saving_the_same_spec_is_a_no_op(setup, store):
    _, make = setup
    _, sched = make()
    sched.save(daily())
    sched.save(daily())
    assert len(store.load(stream_id("digest"))) == 1
    sched.save(daily(overlap="allow"))
    assert len(store.load(stream_id("digest"))) == 2


def test_fire_dataclass_defaults():
    assert Fire("k", None, "manual").files == {}
