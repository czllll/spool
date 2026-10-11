"""How much does durability cost? Append latency of the event store.

    .venv/bin/python scripts/bench_store.py

Appends N events of a realistic size (a tool result of ~2 KB) with
synchronous=FULL (fsync on every commit, the default) and synchronous=NORMAL
(fsync at WAL checkpoints only), and reports per-append latency percentiles.
"""

from __future__ import annotations

import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from spool.store import EventStore  # noqa: E402

N = 2000
PAYLOAD = {"call_id": "c1", "name": "bash", "ok": True, "kind": "executed", "output": "x" * 2000}


def bench(mode: str) -> list[float]:
    with tempfile.TemporaryDirectory() as tmp:
        store = EventStore(Path(tmp) / "bench.db", synchronous=mode)
        store.append("s", "session_started", {"task": "bench"}, expected_seq=0)
        times = []
        for seq in range(1, N + 1):
            t0 = time.perf_counter()
            store.append("s", "tool_completed", PAYLOAD, expected_seq=seq)
            times.append((time.perf_counter() - t0) * 1000)
        store.close()
        return times


def main() -> None:
    for mode in ("FULL", "NORMAL"):
        t = sorted(bench(mode))
        p50, p99 = statistics.median(t), t[int(len(t) * 0.99)]
        print(f"synchronous={mode:<6}  p50 {p50:.3f} ms   p99 {p99:.3f} ms   ({N} appends of ~2 KB)")


if __name__ == "__main__":
    main()
