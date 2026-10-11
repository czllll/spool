"""Standing approval rules: precedence, the Rule-of-Two boundary, runtime, web and CLI."""

import pytest

from spool import events as ev
from spool.models.scripted import ScriptedModel, call, reply
from spool.policy import evaluate
from spool.rules import Rule, RuleBook, RuleError
from spool.runtime import Runtime
from spool.state import State, ToolCall
from spool.tools import PROP_EXTERNAL, PROP_SENSITIVE, PROP_UNTRUSTED, ToolResult, ToolSpec

from .conftest import cfg, run


async def _noop(ctx, args):
    return ToolResult(ok=True, output="ok")


SEND = ToolSpec("mail__send_message", "send", {"type": "object", "properties": {"to": {"type": "string"}}}, _noop,
                properties=frozenset({PROP_EXTERNAL}), confirm=True)
READ = ToolSpec("mail__read_message", "read", {"type": "object", "properties": {}}, _noop,
                properties=frozenset({PROP_UNTRUSTED, PROP_SENSITIVE}))


def rule(effect, tool, **args):
    return Rule(f"r-{effect}-{tool}", effect, tool, args)


def decide(spec, rules, taint=frozenset(), to="amy@corp.example", approval="human"):
    state = State(config=cfg(approval=approval), taint=frozenset(taint), workspace="/tmp")
    return evaluate(ToolCall("c1", spec.name, {"to": to}), spec, state, isolation="process", rules=rules)


def test_precedence_and_argument_patterns():
    assert decide(SEND, [])["rule"] == "confirm"
    allowed = decide(SEND, [rule("allow", "mail__*", to="*@corp.example")])
    assert (allowed["verdict"], allowed["rule"], allowed["user_rule"]["tool"]) == ("allow", "user_rule", "mail__*")
    assert decide(SEND, [rule("allow", "mail__*", to="*@corp.example")], to="x@else.example")["rule"] == "confirm"
    # ask first: an "ask" rule wins over an "allow" rule, and a "deny" wins over both
    both = [rule("allow", "mail__send_message"), rule("ask", "mail__*")]
    assert (decide(SEND, both)["verdict"], decide(SEND, both)["rule"]) == ("ask", "user_rule")
    denied = decide(SEND, both + [rule("deny", "*send*")])
    assert denied["verdict"] == "deny" and denied["user_rule"]["effect"] == "deny"
    # an ask rule asks even for a tool that would otherwise just run
    assert decide(READ, [rule("ask", "mail__read_message")])["verdict"] == "ask"


def test_allow_rules_never_override_the_rule_of_two():
    tainted = {PROP_UNTRUSTED, PROP_SENSITIVE}
    d = decide(SEND, [rule("allow", "mail__send_message")], taint=tainted)
    assert (d["verdict"], d["rule"]) == ("ask", "rule_of_two")
    # ...but a deny rule still blocks, and auto_deny still maps the ask to a deny
    assert decide(SEND, [rule("deny", "mail__send_message")], taint=tainted)["verdict"] == "deny"


def test_rulebook_round_trip(store):
    book = RuleBook(store)
    a = book.add("allow", "shop__place_order", {"sku": "BAT-*"}, note="batteries are fine")
    b = book.add("deny", "calendar__delete_event")
    assert [r.id for r in book.rules()] == [b.id, a.id]  # deny first
    other = RuleBook(store)  # another process sees the same rules
    assert {r.id for r in other.rules()} == {a.id, b.id}
    book.remove(b.id)
    assert [r.id for r in other.rules()] == [a.id]
    with pytest.raises(RuleError):
        book.add("maybe", "x")
    with pytest.raises(KeyError):
        book.remove("nope")
    with pytest.raises(ValueError):  # rule events cannot be written into a session
        store.append("s_1", ev.RULE_ADDED, {"rule": a.to_dict()}, expected_seq=0)


def test_runtime_applies_rules_and_records_them(store, tmp_path, workspace):
    from spool.tools import ToolRegistry

    rt = Runtime(store, ScriptedModel([call("mail__send_message", to="amy@corp.example", call_id="c1"), reply("sent")]),
                 tools=ToolRegistry([SEND]), data_dir=tmp_path / "data")
    rt.rulebook.add("allow", "mail__send_message", {"to": "*@corp.example"})
    sid = rt.start("send", workspace=workspace, config=cfg(approval="human", tools=["mail__send_message"]))
    result = run(rt.run(sid))
    assert result.final_status == "completed"  # no approval needed
    decision = next(e.data for e in store.load(sid) if e.type == ev.POLICY_DECISION)
    assert decision["rule"] == "user_rule" and decision["user_rule"]["args"] == {"to": "*@corp.example"}
