"""Markdown reports for one eval run or a comparison of several."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .metrics import per_task, summarize
from .runner import load_results


def _pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def _num(x: float | None, digits: int = 0) -> str:
    if x is None:
        return "–"
    return f"{x:,.{digits}f}"


def load_run(path: str | Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    path = Path(path)
    spec = json.loads((path / "spec.json").read_text()) if (path / "spec.json").exists() else {"name": path.name}
    return spec, load_results(path / "results.jsonl")


def comparison_table(runs: list[tuple[dict[str, Any], list[dict[str, Any]]]], k: int = 3) -> str:
    head = ("| config | trials | success (95% CI) | pass^3 | steps | input tok | cache reuse | prefill ms/call "
            "| wall s (median) | prefix breaks | tool err |")
    lines = [head, "|" + "---|" * 11]
    for spec, results in runs:
        s = summarize(results, k=k)
        if not s.get("trials"):
            continue
        lo, hi = s["ci95"]
        lines.append(
            f"| {spec.get('name')} | {s['trials']} | {_pct(s['success_rate'])} ({_pct(lo)}–{_pct(hi)}) "
            f"| {_pct(s.get(f'pass^{k}'))} | {_num(s['mean_steps'], 1)} | {_num(s['mean_input_tokens'])} "
            f"| {_pct(s['cache_reuse'])} | {_num(s['mean_prefill_ms_per_call'])} | {_num(s['median_wall_s'])} "
            f"| {_num(s['prefix_breaks_per_session'], 1)} | {_pct(s['tool_error_rate'])} |"
        )
    return "\n".join(lines)


def crash_table(runs: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> str | None:
    rows = []
    for spec, results in runs:
        crashed = [r for r in results if r.get("crashes")]
        if not crashed:
            continue
        completed = sum(1 for r in crashed if r.get("status") == "completed")
        passed = sum(1 for r in crashed if r.get("passed"))
        extra_msgs = sum(max(r.get("outbox_count", 0) - 1, 0) for r in crashed if r.get("outbox_count", 0) > 1)
        rows.append(f"| {spec.get('name')} | {len(crashed)} | {_pct(completed / len(crashed))} "
                    f"| {_pct(passed / len(crashed))} | {extra_msgs} |")
    if not rows:
        return None
    return "\n".join(["| config | crashed trials | finished after resume | passed | extra messages sent |",
                      "|---|---|---|---|---|", *rows])


def audit_table(runs: list[tuple[dict[str, Any], list[dict[str, Any]]]]) -> str | None:
    """Side effects as the services recorded them, checked against the logs (tasks with connectors)."""
    rows = []
    for spec, results in runs:
        a = summarize(results).get("audit")
        if not a:
            continue
        rows.append(f"| {spec.get('name')} | {a['trials']} | {a['side_effects']} | {a['unapproved_side_effects']} "
                    f"| {a['expired_executed']} | {a['untracked_side_effects']} | {a['duplicate_side_effects']} "
                    f"| {a['approvals_asked']} / {a['approvals_granted']} / {a['approvals_expired']} |")
    if not rows:
        return None
    return "\n".join(["| config | trials | writes | without approval | after expiry | untracked | duplicates "
                      "| approvals asked / granted / expired |", "|---|---|---|---|---|---|---|---|", *rows])


def render_report(paths: list[str | Path], title: str = "Eval report") -> str:
    runs = [load_run(p) for p in paths]
    parts = [f"# {title}", ""]
    for spec, _ in runs:
        parts.append(f"- **{spec.get('name')}**: model `{spec.get('model')}`, {spec.get('trials')} trials/task, "
                     f"config `{spec.get('config_source') or 'inline'}`")
    parts += ["", "## Summary", "", comparison_table(runs)]
    crash = crash_table(runs)
    if crash:
        parts += ["", "## Crash recovery", "", crash]
    actions = audit_table(runs)
    if actions:
        parts += ["", "## Side effects (service records vs. the log)", "", actions]
    for spec, results in runs:
        s = summarize(results)
        parts += ["", f"## {spec.get('name')}: per task", "",
                  "| task | passed | median steps | median wall s | failures |", "|---|---|---|---|---|"]
        for row in per_task(results):
            fails = ", ".join(f"{k}×{v}" for k, v in row["failures"].items()) or "–"
            parts.append(f"| {row['task_id']} | {row['passed']}/{row['trials']} | {row['median_steps']:.0f} "
                         f"| {row['median_wall_s']:.0f} | {fails} |")
        if s.get("failures"):
            parts += ["", "Failure categories: " + ", ".join(f"{k} {v}" for k, v in sorted(s["failures"].items()))]
    return "\n".join(parts) + "\n"
