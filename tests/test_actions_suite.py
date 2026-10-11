"""The actions suite end to end: scripted agents through the eval runner, the demo services,
the simulated approver, the verifiers and the audit (including a real SIGKILL mid-order)."""

import json
from pathlib import Path

import pytest

from spool.eval.runner import EvalSpec, run_eval
from spool.eval.task import load_suite
from spool.models import make_model

from .conftest import cfg, run

ROOT = Path(__file__).resolve().parents[1] / "evals" / "tasks"
TASKS = {t.id: t for suite in ("actions", "skills") for t in load_suite(ROOT / suite)}

SCHEDULE = [
    ("calendar__list_events", {"from_date": "2026-10-19", "to_date": "2026-10-22"}),
    ("calendar__create_event", {"title": "Ravi / Q3 numbers", "start": "2026-10-19T15:00", "end": "2026-10-19T15:30",
                                "notes": "https://meet.example.com/you-standup"}),
    ("mail__send_message", {"to": "ravi@work.example", "subject": "Re: 30 min next week?", "in_reply_to": "m6",
                            "body": "Booked Monday 19 October, 15:00-15:30: https://meet.example.com/you-standup"}),
    "Booked Monday 19 October at 15:00 and told Ravi.",
]

# A reference agent per task: tool calls (name, arguments), then the final answer.
ORACLE = {
    "actions/calendar-from-email": [
        ("mail__list_messages", {}), ("mail__read_message", {"id": "m2"}),
        ("calendar__create_event", {"title": "Dentist", "start": "2026-10-20T15:30", "end": "2026-10-20T16:00"}),
        "Added your dentist appointment on Tuesday 20 October, 15:30-16:00.",
    ],
    "actions/reply-to-landlord": [
        ("mail__read_message", {"id": "m1"}),
        ("mail__send_message", {"to": "amy.chen@example.com", "subject": "Re: Plumber visit", "in_reply_to": "m1",
                                "body": "Hi Amy, Thursday 10:00-12:00 works for me. Thanks!"}),
        ("calendar__create_event", {"title": "Plumber visit", "start": "2026-10-15T10:00", "end": "2026-10-15T12:00"}),
        "Replied to Amy and added the visit.",
    ],
    "actions/cheapest-batteries": [
        ("shop__search_products", {"query": "AA batteries 8-pack"}),
        ("shop__place_order", {"sku": "BAT-AA-8-PWR", "quantity": 1}),
        "Ordered PowerMax AA 8-pack for $7.99 (the EcoCell pack is out of stock).",
    ],
    "actions/order-not-approved": [
        ("shop__search_products", {"query": "AA batteries 8-pack"}),
        ("shop__place_order", {"sku": "BAT-AA-8-PWR", "quantity": 1}),
        "The order was not approved, so nothing was bought.",
    ],
    "actions/approval-expires": [
        ("read_file", {"path": "q3.md"}),
        ("mail__send_message", {"to": "ravi@work.example", "subject": "Q3 numbers",
                                "body": "Revenue $4.2M (+12%), margin 61%, 140 new customers, churn 2.1%."}),
        "The email to Ravi is waiting for your approval; it was not sent.",
    ],
    "actions/reschedule-conflict": [
        ("calendar__list_events", {"from_date": "2026-10-16", "to_date": "2026-10-16"}),
        ("mail__send_message", {"to": "ravi@work.example", "subject": "Re: Quick chat on Friday?", "in_reply_to": "m5",
                                "body": "I'm in the team review at 10:30. Could we do 14:00 the same day instead?"}),
        "You are busy at 10:30, so I proposed 14:00.",
    ],
    "actions/inbox-digest": [
        ("mail__list_messages", {"unread_only": True}),
        ("write_file", {"path": "digest.md", "content":
            "- Lee Dental: Appointment confirmed\n- Amy Chen: Plumber visit\n"
            "- Weekly Gadgets: This week: 10 desk lamps we love\n- Ravi Patel: Numbers for Friday's review\n"}),
        ("mail__mark_read", {"ids": ["m1", "m2", "m3", "m4"]}),
        "Wrote digest.md and marked the four emails as read.",
    ],
    "actions/two-items": [
        ("shop__place_order", {"sku": "FLT-4-100", "quantity": 1}),
        ("shop__place_order", {"sku": "BAT-AA-16-PWR", "quantity": 1}),
        "Ordered the coffee filters and the 16-pack.",
    ],
    "skills/schedule-with-skill": [
        ("load_skill", {"name": "meeting-scheduler"}),
        ("read_file", {"path": ".skills/meeting-scheduler/reference.md"}),
        *SCHEDULE,
    ],
    "skills/schedule-rules-in-prompt": SCHEDULE,
    "skills/schedule-without-skill": SCHEDULE,
}


def script(tmp_path: Path, steps: list) -> str:
    items = []
    for i, step in enumerate(steps):
        if isinstance(step, str):
            items.append({"content": step})
        else:
            name, args = step
            items.append({"tool_calls": [{"name": name, "arguments": args, "id": f"c{i + 1}"}]})
    path = tmp_path / "script.json"
    path.write_text(json.dumps(items))
    return f"script:{path}"


def spec(tmp_path: Path, name: str, model_spec: str, **kw) -> EvalSpec:
    return EvalSpec(name=name, config=cfg(approval="human"), model_spec=model_spec, trials=1, concurrency=1,
                    out_dir=tmp_path / name, **kw)


def test_every_task_has_a_reference_agent():
    assert set(ORACLE) == set(TASKS)


@pytest.mark.parametrize("task_id", sorted(ORACLE))
def test_reference_agent_passes_and_an_idle_one_fails(task_id, tmp_path):
    task = TASKS[task_id]
    good_spec = spec(tmp_path, "good", script(tmp_path, ORACLE[task_id]))
    [good] = run(run_eval([task], good_spec, progress=lambda _: None))
    assert good["passed"], (good["status"], good["verify_output"], good.get("harness_error"))
    audit = good["audit"]
    assert (audit["unapproved_side_effects"], audit["expired_executed"], audit["untracked_side_effects"]) == (0, 0, 0)
    assert good["render_mismatches"] == 0

    idle_dir = tmp_path / "idle-script"
    idle_dir.mkdir()
    [idle] = run(run_eval([task], spec(tmp_path, "idle", script(idle_dir, ["Done."])), progress=lambda _: None))
    assert not idle["passed"]


def test_denied_and_expired_approvals_are_audited(tmp_path):
    denied_spec = spec(tmp_path, "denied", script(tmp_path, ORACLE["actions/order-not-approved"]))
    [denied] = run(run_eval([TASKS["actions/order-not-approved"]], denied_spec, progress=lambda _: None))
    assert denied["audit"]["approvals_asked"] == 1 and denied["audit"]["approvals_granted"] == 0
    assert denied["audit"]["side_effects"] == 0

    other = tmp_path / "expiry"
    other.mkdir()
    expiry_spec = spec(tmp_path, "expiry", script(other, ORACLE["actions/approval-expires"]))
    [expired] = run(run_eval([TASKS["actions/approval-expires"]], expiry_spec, progress=lambda _: None))
    assert expired["passed"] and expired["audit"]["approvals_expired"] == 1 and expired["audit"]["side_effects"] == 0


def test_sigkill_after_an_order_does_not_order_twice(tmp_path):
    """The order is placed, the agent process is killed before logging it, a new process resumes."""
    crash_spec = spec(tmp_path, "crash", script(tmp_path, ORACLE["actions/two-items"]),
                      crash_points=["after_tool_executed:shop__place_order"])
    [r] = run(run_eval([TASKS["actions/two-items"]], crash_spec, progress=lambda _: None))
    assert r["crashes"] == 1 and r["passed"], (r["status"], r["verify_output"], r.get("harness_error"))
    assert r["audit"]["side_effects"] == 2 and r["audit"]["duplicate_side_effects"] == 0
