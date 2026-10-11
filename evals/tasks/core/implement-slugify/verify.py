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

cases = {"Hello, World!": "hello-world", "  Spool: event-sourced agents  ": "spool-event-sourced-agents",
         "a--b": "a-b", "2026 Q3 Plan": "2026-q3-plan", "!!!": "untitled", "": "untitled",
         "Caf\u00e9 Ol\u00e9": "caf-ol"}
code = "import json,sys; from text_utils import slugify; print(json.dumps({k: slugify(k) for k in json.loads(sys.argv[1])}))"
r = subprocess.run([sys.executable, "-c", code, json.dumps(list(cases))], cwd=WS, capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    fail("slugify raised: " + r.stderr[-300:])
got = json.loads(r.stdout)
bad = {k: (got[k], v) for k, v in cases.items() if got[k] != v}
if bad:
    fail(f"wrong results: {bad}")
ok()
