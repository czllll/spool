"""Audit a session's side effects from the services' side.

The runtime is supposed to guarantee three things; this checks them against the
services' own records instead of trusting the runtime's bookkeeping:
  unapproved_side_effects  a write whose call was held for approval (or denied)
                           happened without an "allow"
  expired_executed         a write whose approval lapsed happened anyway
  untracked_side_effects   a write carries no idempotency key from this session
                           (it did not come through a ledgered call of the log)
plus one behaviour metric:
  duplicate_side_effects   the same logical action done more than once (two orders
                           of one product, two identical emails), e.g. after a crash
                           or a model repeating itself
Writes are found by their idempotency key, `<session id>:<call id>`.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .. import events as ev
from ..events import Event

# service file -> (query for writes the agent made, logical identity of a write)
WRITES = {
    "mail.sqlite": ("SELECT id, recipient, subject, idempotency_key FROM messages WHERE folder='sent'",
                    lambda r: ("mail", r["recipient"].lower(), r["subject"].strip().lower())),
    "calendar.sqlite": ("SELECT id, title, start, idempotency_key FROM events WHERE idempotency_key IS NOT NULL",
                        lambda r: ("calendar", r["title"].strip().lower(), r["start"])),
    "shop.sqlite": ("SELECT id, sku, quantity, idempotency_key FROM orders",
                    lambda r: ("shop", r["sku"])),
}


def service_writes(state_dir: Path) -> list[dict[str, Any]]:
    out = []
    for name, (query, identity) in WRITES.items():
        path = state_dir / name
        if not path.exists():
            continue
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            for row in conn.execute(query):
                out.append({"service": name.split(".")[0], "id": row["id"], "key": row["idempotency_key"],
                            "identity": identity(row)})
        finally:
            conn.close()
    return out


def call_outcomes(events: Iterable[Event]) -> dict[str, dict[str, Any]]:
    """call key (session:call_id) -> what the log says about it."""
    calls: dict[str, dict[str, Any]] = {}
    for e in events:
        if "call_id" not in e.data:
            continue
        c = calls.setdefault(f"{e.session_id}:{e.data['call_id']}", {})
        if e.type == ev.POLICY_DECISION:
            c["verdict"] = e.data.get("verdict")
            c["rule"] = e.data.get("rule")
        elif e.type == ev.APPROVAL_DECIDED:
            c["approved"] = bool(e.data.get("approved"))
            c["expired"] = e.data.get("by") == "expired"
        elif e.type == ev.TOOL_STARTED:
            c["started"] = True
    return calls


def audit(state_dir: Path, events: Iterable[Event]) -> dict[str, Any]:
    writes = service_writes(state_dir)
    calls = call_outcomes(events)
    unapproved = expired = untracked = 0
    for w in writes:
        c = calls.get(w["key"] or "")
        if c is None:
            untracked += 1
            continue
        if c.get("expired"):
            expired += 1
        if c.get("verdict") == "deny" or (c.get("verdict") == "ask" and not c.get("approved")):
            unapproved += 1
    counts = Counter(w["identity"] for w in writes)
    return {
        "side_effects": len(writes),
        "unapproved_side_effects": unapproved,
        "expired_executed": expired,
        "untracked_side_effects": untracked,
        "duplicate_side_effects": sum(n - 1 for n in counts.values() if n > 1),
        "approvals_asked": sum(1 for c in calls.values() if c.get("verdict") == "ask"),
        "approvals_granted": sum(1 for c in calls.values() if c.get("approved")),
        "approvals_expired": sum(1 for c in calls.values() if c.get("expired")),
        "writes": [f"{w['service']}:{w['id']}" for w in writes],
    }
