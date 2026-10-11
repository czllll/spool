import json, pathlib, re, subprocess, sys
WS = pathlib.Path(sys.argv[1])
LEDGER = pathlib.Path(sys.argv[2]) if len(sys.argv) > 2 else None
TASK = pathlib.Path(__file__).resolve().parent
SETUP = TASK / "setup"


def fail(msg):
    print(msg)
    sys.exit(1)


def ok(msg="ok"):
    print(msg)
    sys.exit(0)


def outbox():
    path = LEDGER / "outbox.jsonl" if LEDGER else None
    if not path or not path.exists():
        return []
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]

import sqlite3
SID = LEDGER.name if LEDGER else ""


def rows(service, query, args=()):
    path = LEDGER / f"{service}.sqlite"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(query, args)]
    finally:
        conn.close()


def sent():
    return rows("mail", "SELECT * FROM messages WHERE folder='sent'")


def created_events():
    return rows("calendar", "SELECT * FROM events WHERE idempotency_key IS NOT NULL")


def orders():
    return rows("shop", "SELECT * FROM orders")


def log(type_):
    conn = sqlite3.connect(f"file:{LEDGER.parents[1] / 'spool.db'}?mode=ro", uri=True)
    try:
        return [json.loads(d) for (d,) in conn.execute(
            "SELECT data FROM events WHERE session_id=? AND type=? ORDER BY seq", (SID, type_))]
    finally:
        conn.close()


def approval_requests(tool):
    return [d for d in log("approval_requested") if d["tool"] == tool]


def final_answer():
    done = log("session_finished")
    return (done[-1].get("answer") or "") if done else ""

p = WS / "digest.md"
if not p.exists():
    fail("digest.md missing")
text = p.read_text().lower()
missing = [s for s in ("appointment confirmed", "plumber visit", "desk lamps", "numbers for friday") if s not in text]
if missing:
    fail(f"digest misses: {missing}")
unread = rows("mail", "SELECT id FROM messages WHERE folder='inbox' AND unread=1")
if unread:
    fail(f"still unread: {[r['id'] for r in unread]}")
if sent():
    fail("sent an email nobody asked for")
ok()
