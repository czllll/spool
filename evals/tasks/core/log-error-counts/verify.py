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

counts = {}
for line in (SETUP / "app.log").read_text().splitlines():
    parts = line.split()
    if parts[1] == "ERROR":
        counts[parts[2]] = counts.get(parts[2], 0) + 1
want = [f"{m}: {c}" for m, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
p = WS / "errors.txt"
if not p.exists():
    fail("errors.txt missing")
got = [l.strip() for l in p.read_text().splitlines() if l.strip()]
if got != want:
    fail(f"got {got} want {want}")
ok()
