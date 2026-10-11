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
want = {"high": 0, "medium": 0, "low": 0}
with open(SETUP / "tickets.csv") as fh:
    for row in csv.DictReader(fh):
        if row["status"] == "open":
            want[row["priority"]] += 1
msgs = [m for m in outbox() if m.get("to", "").strip().lower() == "team@example.com"]
if len(msgs) != 1:
    fail(f"expected exactly one message to team@example.com, found {len(msgs)}")
m = msgs[0]
if "weekly ticket report" not in m.get("subject", "").lower():
    fail(f"subject was {m.get('subject')!r}")
for prio, n in want.items():
    found = re.search(rf"{prio}\s*:\s*(\d+)", m["body"], re.I)
    if not found or int(found.group(1)) != n:
        fail(f"{prio}: expected {n}, body was {m['body']!r}")
ok()
