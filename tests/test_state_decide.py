import pytest

from spool import events as ev
from spool.config import AgentConfig
from spool.decide import (
    Authorize,
    CallModel,
    CompleteWithoutRunning,
    Done,
    ExecuteTool,
    Finish,
    RequestApproval,
    Sleep,
    Wake,
    WaitForApproval,
    decide,
)
from spool.events import Event
from spool.state import State, reduce, replay


class Log:
    """Builds an event list with dense seqs."""

    def __init__(self, **config):
        self.events = []
        self.add(ev.SESSION_STARTED, task="t", config=AgentConfig(**config).to_dict(), workspace="/w")
        self.add(ev.USER_MESSAGE, text="t")

    def add(self, type, **data):
        self.events.append(Event("s", len(self.events), type, data))
        return self

    def response(self, *calls, content=""):
        tool_calls = [{"id": cid, "name": name, "arguments": args, "raw_arguments": None, "parse_error": None}
                      for cid, name, args in calls]
        return self.add(ev.MODEL_RESPONSE, message={"content": content, "tool_calls": tool_calls},
                        usage={"input_tokens": 100, "output_tokens": 10, "cached_tokens": 50}, latency_ms=5)

    @property
    def state(self) -> State:
        return replay(self.events)


def test_reduce_is_deterministic_and_incremental_equals_replay():
    log = Log().response(("c1", "bash", {"command": "ls"}))
    log.add(ev.POLICY_DECISION, call_id="c1", verdict="allow", properties=["sensitive_data"])
    log.add(ev.TOOL_STARTED, call_id="c1", name="bash", signature="x")
    log.add(ev.TOOL_COMPLETED, call_id="c1", name="bash", ok=True, kind="executed", output="a")
    s = State()
    for e in log.events:
        s = reduce(s, e)
    assert s == replay(log.events)
    assert s.taint == frozenset({"sensitive_data"})
    assert s.usage.input_tokens == 100 and s.usage.cached_tokens == 50 and s.usage.model_calls == 1
    assert s.pending == () and s.in_flight is None and s.tool_calls == 1


def test_reduce_rejects_gaps():
    log = Log()
    with pytest.raises(ValueError):
        reduce(replay(log.events[:1]), log.events[1].__class__("s", 5, ev.USER_MESSAGE, {"text": "x"}))


def test_decide_walks_a_tool_call_through_policy():
    log = Log().response(("c1", "read_file", {"path": "a"}))
    assert isinstance(decide(log.state, now=0), Authorize)
    log.add(ev.POLICY_DECISION, call_id="c1", verdict="allow", properties=[])
    action = decide(log.state, now=0)
    assert isinstance(action, ExecuteTool) and not action.recovering
    log.add(ev.TOOL_STARTED, call_id="c1", name="read_file", signature="s")
    action = decide(log.state, now=0)
    assert isinstance(action, ExecuteTool) and action.recovering  # crash right here -> recovery
    log.add(ev.TOOL_COMPLETED, call_id="c1", name="read_file", ok=True, kind="executed", output="x")
    assert isinstance(decide(log.state, now=0), CallModel)


def test_decide_denied_and_asked_calls():
    log = Log().response(("c1", "send_message", {"to": "a", "body": "b"}), ("c2", "bash", {"command": "ls"}))
    log.add(ev.POLICY_DECISION, call_id="c1", verdict="ask", reason="rule", properties=[])
    action = decide(log.state, now=0)
    assert isinstance(action, RequestApproval) and action.call.id == "c1"
    log.add(ev.APPROVAL_REQUESTED, call_id="c1")
    assert isinstance(decide(log.state, now=0), WaitForApproval)
    log.add(ev.APPROVAL_DECIDED, call_id="c1", approved=False)
    action = decide(log.state, now=0)
    assert isinstance(action, CompleteWithoutRunning) and action.kind == "rejected"


def test_decide_invalid_arguments_never_run():
    log = Log()
    log.add(ev.MODEL_RESPONSE, message={"content": "", "tool_calls": [
        {"id": "c1", "name": "bash", "arguments": {}, "raw_arguments": "{oops", "parse_error": "bad json"}]},
        usage={}, latency_ms=1)
    action = decide(log.state, now=0)
    assert isinstance(action, CompleteWithoutRunning) and action.kind == "invalid"


def test_decide_finish_conditions():
    log = Log().response(content="all done")
    action = decide(log.state, now=0)
    assert isinstance(action, Finish) and action.status == "completed" and action.answer == "all done"

    log = Log(max_steps=1).response(("c1", "bash", {"command": "ls"}))
    log.add(ev.POLICY_DECISION, call_id="c1", verdict="allow", properties=[])
    log.add(ev.TOOL_COMPLETED, call_id="c1", name="bash", ok=True, kind="executed", output="")
    action = decide(log.state, now=0)
    assert isinstance(action, Finish) and action.status == "max_steps"

    log = Log(max_model_errors=2)
    log.add(ev.MODEL_ERROR, error="x").add(ev.MODEL_ERROR, error="y")
    action = decide(log.state, now=0)
    assert isinstance(action, Finish) and action.status == "failed"

    log.add(ev.SESSION_FINISHED, status="failed")
    assert isinstance(decide(log.state, now=0), Done)


def test_plan_only_turns_can_be_free():
    def log_with_plans(**config):
        log = Log(max_steps=2, **config)
        for i in range(2):
            log.response((f"t{i}", "todo_write", {"todos": []}))
            log.add(ev.POLICY_DECISION, call_id=f"t{i}", verdict="allow", properties=[])
            log.add(ev.TOOL_COMPLETED, call_id=f"t{i}", name="todo_write", ok=True, kind="executed", output="")
        return log

    assert log_with_plans().state.plan_only_steps == 2
    assert isinstance(decide(log_with_plans(free_plan_steps=0).state, now=0), Finish)     # charged
    assert isinstance(decide(log_with_plans().state, now=0), CallModel)                   # default: 5 free
    assert isinstance(decide(log_with_plans(free_plan_steps=2).state, now=0), CallModel)  # free
    assert isinstance(decide(log_with_plans(free_plan_steps=1).state, now=0), CallModel)  # 2 - 1 < 2


def test_sleep_and_wake():
    log = Log().response(("c1", "sleep", {"seconds": 60}))
    log.add(ev.POLICY_DECISION, call_id="c1", verdict="allow", properties=[])
    log.add(ev.TOOL_COMPLETED, call_id="c1", name="sleep", ok=True, kind="executed", output="", wake_at=1000.0)
    assert log.state.status == "sleeping"
    assert isinstance(decide(log.state, now=999), Sleep)
    assert isinstance(decide(log.state, now=1000), Wake)
    log.add(ev.NOTE, kind="woke")
    assert log.state.status == "running" and isinstance(decide(log.state, now=1000), CallModel)


def test_follow_up_message_reopens_session():
    log = Log().response(content="done")
    log.add(ev.SESSION_FINISHED, status="completed", answer="done")
    assert log.state.status == "finished"
    log.add(ev.USER_MESSAGE, text="one more thing")
    assert log.state.status == "running" and isinstance(decide(log.state, now=0), CallModel)


def test_denied_calls_do_not_taint():
    log = Log().response(("c1", "web_fetch", {"url": "u"}))
    log.add(ev.POLICY_DECISION, call_id="c1", verdict="deny", properties=["untrusted_input", "external_comm"])
    log.add(ev.TOOL_COMPLETED, call_id="c1", name="web_fetch", ok=False, kind="denied", output="")
    s = log.state
    assert s.taint == frozenset() and s.denied_calls == 1
