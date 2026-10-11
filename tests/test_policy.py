import dataclasses

import pytest

from spool import policy
from spool.config import AgentConfig
from spool.state import State, ToolCall
from spool.tools import PROP_EXTERNAL, PROP_SENSITIVE, PROP_UNTRUSTED
from spool.tools.builtin import builtin_registry

REG = builtin_registry()


def state(tmp_path, taint=(), **config):
    return State(session_id="s", workspace=str(tmp_path), config=AgentConfig(**config), taint=frozenset(taint),
                 status="running")


def verdict(st, name, isolation="seatbelt", **args):
    return policy.evaluate(ToolCall("c", name, args), REG.get(name), st, isolation=isolation)


def test_properties_of_calls(tmp_path):
    st = state(tmp_path)
    assert set(verdict(st, "read_file", path="secrets/key.txt")["properties"]) == {PROP_SENSITIVE}
    assert set(verdict(st, "read_file", path="downloads/readme.md")["properties"]) == {PROP_UNTRUSTED}
    assert verdict(st, "read_file", path="notes.md")["properties"] == []
    assert set(verdict(st, "web_fetch", url="https://x")["properties"]) == {PROP_UNTRUSTED, PROP_EXTERNAL}
    assert set(verdict(st, "send_message", to="a", body="b")["properties"]) == {PROP_EXTERNAL}
    # writing into a protected dir is refused by the tool itself; it is not a read
    assert verdict(st, "write_file", path="secrets/x", content="y")["properties"] == []


@pytest.mark.parametrize("taint,name,args,blocked", [
    ((), "web_fetch", {"url": "u"}, False),                                   # A+C
    ((PROP_UNTRUSTED, PROP_EXTERNAL), "read_file", {"path": "secrets/k"}, True),  # +B -> all three
    ((PROP_SENSITIVE,), "web_fetch", {"url": "u"}, True),                     # B + A+C
    ((PROP_SENSITIVE,), "send_message", {"to": "a", "body": "b"}, False),     # B+C only
    ((PROP_UNTRUSTED,), "send_message", {"to": "a", "body": "b"}, False),     # A+C only
    ((PROP_UNTRUSTED, PROP_SENSITIVE), "send_message", {"to": "a", "body": "b"}, True),
    ((PROP_UNTRUSTED, PROP_SENSITIVE, PROP_EXTERNAL), "read_file", {"path": "notes.md"}, False),  # adds nothing
])
def test_rule_of_two(tmp_path, taint, name, args, blocked):
    d = verdict(state(tmp_path, taint, approval="auto_deny"), name, **args)
    assert (d["verdict"] == "deny") is blocked
    if blocked:
        assert d["rule"] == "rule_of_two" and "Rule of Two" in d["reason"]


def test_approval_modes(tmp_path):
    taint = (PROP_UNTRUSTED, PROP_EXTERNAL)
    for mode, expected in [("human", "ask"), ("auto_deny", "deny"), ("auto_approve", "allow")]:
        d = verdict(state(tmp_path, taint, approval=mode), "read_file", path="secrets/k")
        assert d["verdict"] == expected and d["rule"] == "rule_of_two"


def test_policy_off_still_records_properties(tmp_path):
    d = verdict(state(tmp_path, (PROP_UNTRUSTED, PROP_EXTERNAL), policy="off"), "read_file", path="secrets/k")
    assert d["verdict"] == "allow" and d["rule"] == "policy_off" and d["properties"] == [PROP_SENSITIVE]


def test_unsandboxed_bash_counts_as_sensitive(tmp_path):
    st = state(tmp_path)
    assert verdict(st, "bash", isolation="seatbelt", command="ls")["properties"] == []
    assert verdict(st, "bash", isolation="process", command="ls")["properties"] == [PROP_SENSITIVE]
    no_secrets = dataclasses.replace(st, config=AgentConfig(sensitive_paths=()))
    assert verdict(no_secrets, "bash", isolation="process", command="ls")["properties"] == []
