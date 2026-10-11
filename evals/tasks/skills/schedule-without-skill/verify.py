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

from datetime import datetime
events = created_events()
if len(events) != 1:
    fail(f"expected 1 new event, found {len(events)}")
e = events[0]
start, end = (datetime.strptime(e[k], "%Y-%m-%dT%H:%M") for k in ("start", "end"))
if not (datetime(2026, 10, 19) <= start < datetime(2026, 10, 24)):
    fail(f"not in the week of 19 October: {e['start']}")
if start.weekday() > 3:
    fail(f"booked on a {start:%A}; meetings are Monday to Thursday")
in_hours = (start.hour >= 10 and end <= start.replace(hour=12, minute=0)) or \
           (start.hour >= 14 and end <= start.replace(hour=17, minute=0))
if not in_hours or (end - start).seconds != 1800:
    fail(f"{e['start']}-{e['end']} is not a 30-minute slot inside 10-12 or 14-17")
busy = rows("calendar", "SELECT start, end FROM events WHERE idempotency_key IS NULL")
if any(b["start"] < e["end"] and e["start"] < b["end"] for b in busy):
    fail(f"{e['start']} overlaps an existing event")
if not re.match(r"ravi\s*/\s*\S", e["title"].strip().lower()):
    fail(f"title {e['title']!r} is not '<Person> / <topic>'")
if "meet.example.com/you-standup" not in (e["notes"] or ""):
    fail("the meeting link is not in the event notes")
mails = [m for m in sent() if "ravi@work.example" in m["recipient"].lower()]
if len(mails) != 1 or start.strftime("%H:%M") not in mails[0]["body"]:
    fail("Ravi was not told the time")
ok()
