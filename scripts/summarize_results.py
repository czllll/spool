"""Headline tables for the README, computed from evals/results/* (results.jsonl + event logs).

    .venv/bin/python scripts/summarize_results.py > evals/results/HEADLINE.md
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spool import events as ev  # noqa: E402
from spool.eval.metrics import pass_hat_k, wilson_interval  # noqa: E402
from spool.eval.runner import load_results  # noqa: E402
from spool.store import EventStore  # noqa: E402

R = Path(__file__).resolve().parents[1] / "evals" / "results"


def pct(x: float | None) -> str:
    return "–" if x is None else f"{100 * x:.0f}%"


def rows(name: str, prefix: str | None = None) -> list[dict]:
    out = load_results(R / name / "results.jsonl")
    return [r for r in out if prefix is None or r["task_id"].startswith(prefix)]


def peak_prompt(name: str, sid: str, store_cache: dict) -> int:
    store = store_cache.setdefault(name, EventStore(R / name / "spool.db"))
    tokens = [(e.data.get("usage") or {}).get("input_tokens", 0) for e in store.load(sid) if e.type == ev.MODEL_RESPONSE]
    return max(tokens, default=0)


def line(label: str, rs: list[dict], stores: dict, name: str) -> str:
    if not rs:
        return f"| {label} | 0 | – | – | – | – | – | – |"
    n, c = len(rs), sum(r["passed"] for r in rs)
    lo, hi = wilson_interval(c, n)
    by_task: dict[str, list[dict]] = {}
    for r in rs:
        by_task.setdefault(r["task_id"], []).append(r)
    hat = [pass_hat_k(len(t), sum(x["passed"] for x in t), 3) for t in by_task.values() if len(t) >= 3]
    cached = sum(r["cached_tokens"] for r in rs)
    total = sum(r["input_tokens"] for r in rs)
    prefill = [r["prefill_ms_per_call"] for r in rs if r.get("prefill_ms_per_call") is not None]
    peaks = [peak_prompt(name, r["session_id"], stores) for r in rs]
    cells = [
        label,
        str(n),
        f"{pct(c / n)} ({pct(lo)}–{pct(hi)})",
        pct(statistics.mean(hat)) if hat else "–",
        pct(cached / total if total else 0),
        f"{statistics.mean(prefill):,.0f}" if prefill else "–",
        f"{statistics.median(r['wall_ms'] for r in rs) / 1000:,.0f}",
        f"{statistics.median(peaks):,.0f}",
    ]
    return "| " + " | ".join(cells) + " |"


def main() -> None:
    stores: dict = {}
    head = ("| configuration | trials | success (95% CI) | pass^3 | prompt tokens from cache | prefill ms per call "
            "| median wall s | median peak prompt |")
    sep = "|---|---|---|---|---|---|---|---|"
    print("### Baseline (default harness)\n")
    print(head)
    print(sep)
    print(line("core tasks (12), 15-min budget", rows("baseline", "core/"), stores, "baseline"))
    print(line("long tasks (4), 15-min budget", rows("baseline", "long/"), stores, "baseline"))
    print(line("long tasks (4), 40-min budget", rows("ctx_offload"), stores, "ctx_offload"))

    print("\n### E1: prefix stability (core tasks)\n")
    print(head)
    print(sep)
    print(line("stable prefix (baseline)", rows("baseline", "core/"), stores, "baseline"))
    print(line("clock at top of system prompt", rows("unstable_prefix"), stores, "unstable_prefix"))

    print("\n### Failure 0002 follow-ups: too many plan updates (core tasks)\n")
    print(head)
    print(sep)
    print(line("baseline prompt", rows("baseline", "core/"), stores, "baseline"))
    print(line("baseline_v2 prompt", rows("baseline_v2"), stores, "baseline_v2"))
    print(line("baseline_v3: plan-only turns not charged", rows("baseline_v3"), stores, "baseline_v3"))

    print("\n### E2: context strategy (long tasks, 40-min budget)\n")
    print(head)
    print(sep)
    print(line("full history", rows("ctx_full"), stores, "ctx_full"))
    print(line("fixed-threshold offload (old default)", rows("ctx_offload"), stores, "ctx_offload"))
    print(line("reversible fold", rows("ctx_fold"), stores, "ctx_fold"))
    print(line("summarize", rows("ctx_summarize"), stores, "ctx_summarize"))
    print(line("budget-aware offload (new default)", rows("ctx_adaptive"), stores, "ctx_adaptive"))

    print("\n### E4: crash recovery (SIGKILL, then resume in a new process)\n")
    print("| task | trials | crashed | finished after resume | passed | messages sent per trial |")
    print("|---|---|---|---|---|---|")
    for name in ("crash", "crash_more"):
        rs = rows(name)
        if not rs:
            continue
        crashed = [r for r in rs if r.get("crashes")]
        print(f"| {rs[0]['task_id']} | {len(rs)} | {len(crashed)} | {sum(r['status'] == 'completed' for r in crashed)}/{len(crashed)} "
              f"| {sum(r['passed'] for r in crashed)}/{len(crashed)} | {', '.join(str(r['outbox_count']) for r in rs)} |")

    print("\n### Replay check\n")
    checked = mismatches = 0
    for d in sorted(R.iterdir()):
        for r in load_results(d / "results.jsonl"):
            checked += r.get("render_checked", 0)
            mismatches += r.get("render_mismatches", 0)
    print(f"Every recorded request re-rendered from the log: {checked:,} requests, {mismatches} mismatches.")


if __name__ == "__main__":
    main()
