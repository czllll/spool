"""Aggregate eval metrics.

pass@k  : probability that at least one of k trials succeeds (capability).
pass^k  : probability that all k trials succeed (reliability; from tau-bench).
Both use the unbiased estimators over n >= k trials of a task:
    pass@k = 1 - C(n-c, k) / C(n, k)        pass^k = C(c, k) / C(n, k)
"""

from __future__ import annotations

import math
import statistics
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping


def pass_at_k(n: int, c: int, k: int) -> float:
    if n < k:
        raise ValueError("need n >= k")
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


def pass_hat_k(n: int, c: int, k: int) -> float:
    if n < k:
        raise ValueError("need n >= k")
    return math.comb(c, k) / math.comb(n, k)


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


def failure_category(r: Mapping[str, Any]) -> str | None:
    if r.get("passed"):
        return None
    if r.get("harness_error"):
        return "harness_error"
    status = r.get("status")
    if status == "max_steps":
        return "ran_out_of_steps"
    if status in ("failed", "budget_exhausted"):
        return "model_or_budget_error"
    if status in ("timeout", "awaiting_approval", "sleeping", "paused"):
        return f"did_not_finish:{status}"
    if r.get("denied_calls", 0) > 0:
        return "blocked_by_policy"
    if r.get("loop_warnings", 0) > 0:
        return "looping"
    if status == "completed":
        return "wrong_result_claimed_done"
    return "other"


def summarize(results: Iterable[Mapping[str, Any]], k: int = 3) -> dict[str, Any]:
    rows = list(results)
    if not rows:
        return {"trials": 0}
    by_task: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in rows:
        by_task[r["task_id"]].append(r)
    n_pass = sum(1 for r in rows if r.get("passed"))
    lo, hi = wilson_interval(n_pass, len(rows))
    hat, at = [], []
    for trials in by_task.values():
        n, c = len(trials), sum(1 for t in trials if t.get("passed"))
        if n >= k:
            hat.append(pass_hat_k(n, c, k))
            at.append(pass_at_k(n, c, k))
    in_tok = sum(r.get("input_tokens", 0) for r in rows)
    cached = sum(r.get("cached_tokens", 0) for r in rows)
    tool_calls = sum(r.get("tool_calls", 0) for r in rows)
    summary: dict[str, Any] = {
        "trials": len(rows),
        "tasks": len(by_task),
        "passed": n_pass,
        "success_rate": n_pass / len(rows),
        "ci95": [lo, hi],
        f"pass^{k}": statistics.mean(hat) if hat else None,
        f"pass@{k}": statistics.mean(at) if at else None,
        "mean_steps": statistics.mean(r.get("steps", 0) for r in rows),
        "mean_input_tokens": statistics.mean(r.get("input_tokens", 0) for r in rows),
        "mean_output_tokens": statistics.mean(r.get("output_tokens", 0) for r in rows),
        "cache_reuse": cached / in_tok if in_tok else 0.0,
        "mean_wall_s": statistics.mean(r.get("wall_ms", 0) for r in rows) / 1000,
        "median_wall_s": statistics.median(r.get("wall_ms", 0) for r in rows) / 1000,
        "mean_prefill_ms_per_call": _mean_or_none(r.get("prefill_ms_per_call") for r in rows),
        "prefix_breaks_per_session": statistics.mean(r.get("prefix_breaks", 0) for r in rows),
        "tool_error_rate": (sum(r.get("tool_errors", 0) for r in rows) / tool_calls) if tool_calls else 0.0,
        "mean_cost_usd": statistics.mean(r.get("cost_usd", 0.0) for r in rows),
        "failures": dict(Counter(c for r in rows if (c := failure_category(r)))),
    }
    summary["policy_blocked_trials"] = sum(1 for r in rows if r.get("denied_calls", 0) > 0)
    audited = [r["audit"] for r in rows if r.get("audit")]
    if audited:
        summary["audit"] = {key: sum(a.get(key, 0) for a in audited) for key in AUDIT_KEYS}
        summary["audit"]["trials"] = len(audited)
    crashed = [r for r in rows if r.get("crashes", 0) > 0]
    if crashed:
        summary["crash_trials"] = len(crashed)
        summary["recovered_rate"] = sum(1 for r in crashed if r.get("status") in ("completed",)) / len(crashed)
        summary["duplicate_side_effects"] = sum(r.get("duplicate_effects", 0) for r in rows)
    return summary


AUDIT_KEYS = ("side_effects", "unapproved_side_effects", "expired_executed", "untracked_side_effects",
              "duplicate_side_effects", "approvals_asked", "approvals_granted", "approvals_expired")


def per_task(results: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    by_task: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for r in results:
        by_task[r["task_id"]].append(r)
    out = []
    for task_id, trials in sorted(by_task.items()):
        out.append({
            "task_id": task_id,
            "passed": sum(1 for t in trials if t.get("passed")),
            "trials": len(trials),
            "median_steps": statistics.median(t.get("steps", 0) for t in trials),
            "median_wall_s": statistics.median(t.get("wall_ms", 0) for t in trials) / 1000,
            "failures": dict(Counter(c for t in trials if (c := failure_category(t)))),
        })
    return out


def _mean_or_none(values: Iterable[Any]) -> float | None:
    vals = [v for v in values if v is not None]
    return statistics.mean(vals) if vals else None
