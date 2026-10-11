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

import csv
users = sorted(json.loads((SETUP / "users.json").read_text()), key=lambda u: u["id"])
p = WS / "users.csv"
if not p.exists():
    fail("users.csv missing")
rows = list(csv.reader(p.read_text().splitlines()))
if not rows or rows[0] != ["id", "name", "email", "plan"]:
    fail(f"bad header {rows[:1]}")
want = [[str(u["id"]), u["name"], u["email"], u["plan"]] for u in users]
if rows[1:] != want:
    fail("rows differ from expected (order or content)")
ok()
