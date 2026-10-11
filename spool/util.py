"""Small helpers shared across modules."""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime, timezone
from typing import Any


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8 kept as-is.

    Everything that ends up in a model request or in the event log goes through
    this, so two renders of the same data are byte-identical (a precondition for
    prefix caching and for replay checks).
    """
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def digest(obj: Any, length: int = 64) -> str:
    return hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()[:length]


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def now() -> float:
    return time.time()


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def truncate_middle(text: str, limit: int, marker: str = "\n... [{omitted} chars omitted] ...\n") -> str:
    """Keep the head and tail of a long string; the middle is usually least useful."""
    if len(text) <= limit:
        return text
    keep = max(limit - 64, 0)
    head = keep * 2 // 3
    tail = keep - head
    omitted = len(text) - head - tail
    return text[:head] + marker.format(omitted=omitted) + (text[-tail:] if tail else "")


def first_line(text: str, limit: int = 100) -> str:
    line = text.strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 1] + "…"
