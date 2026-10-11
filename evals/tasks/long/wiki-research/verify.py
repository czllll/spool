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

p = WS / "answer.md"
if not p.exists():
    fail("answer.md missing")
text = p.read_text().lower()
checks = {"rotation": r"rotation\s*:\s*6\b", "database": r"database\s*:\s*(postgresql\s*)?16\.4",
          "approver": r"approver\s*:.*data council"}
bad = [k for k, rx in checks.items() if not re.search(rx, text)]
if bad:
    fail(f"wrong or missing: {bad}; answer was {p.read_text()[:200]!r}")
ok()
