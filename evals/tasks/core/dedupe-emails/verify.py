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

seen, want = set(), []
for line in (SETUP / "emails.txt").read_text().splitlines():
    e = line.strip().lower()
    if e and e not in seen:
        seen.add(e)
        want.append(e)
p = WS / "unique.txt"
if not p.exists():
    fail("unique.txt missing")
got = [l.strip() for l in p.read_text().splitlines() if l.strip()]
if got != want:
    fail(f"got {got[:5]}... want {want[:5]}...")
ok()
