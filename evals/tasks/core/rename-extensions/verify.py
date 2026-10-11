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

for name in ["a", "b", "meeting-2026-03", "todo"]:
    if (WS / "notes" / f"{name}.txt").exists():
        fail(f"notes/{name}.txt still exists")
    md = WS / "notes" / f"{name}.md"
    if not md.exists():
        fail(f"notes/{name}.md missing")
    if md.read_text() != (SETUP / "notes" / f"{name}.txt").read_text():
        fail(f"notes/{name}.md content changed")
if (WS / "notes" / "ideas.md").read_text() != (SETUP / "notes" / "ideas.md").read_text():
    fail("ideas.md was modified")
ok()
