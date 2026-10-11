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

want = (SETUP / "server.ini").read_text().replace("port = 8000", "port = 8080").replace("debug = false", "debug = true")
got = (WS / "server.ini").read_text()
if got.rstrip("\n") != want.rstrip("\n"):
    fail("server.ini differs from the expected edit")
ok()
