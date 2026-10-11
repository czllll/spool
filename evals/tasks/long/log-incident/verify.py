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

lines = (SETUP / "server.log").read_text().splitlines()
first = next(l for l in lines if "level=ERROR" in l and "code=E1442" in l)
req = re.search(r"req=(\S+)", first).group(1)
user = re.search(r"user=(\S+)", first).group(1)
count = sum(1 for l in lines if "level=ERROR" in l and f"user={user} " in l)
p = WS / "incident.json"
if not p.exists():
    fail("incident.json missing")
try:
    got = json.loads(p.read_text())
except Exception as exc:
    fail(f"invalid JSON: {exc}")
if got.get("request_id") != req:
    fail(f"request_id {got.get('request_id')} != {req}")
if got.get("user_id") != user:
    fail(f"user_id {got.get('user_id')} != {user}")
if int(got.get("user_error_count", -1)) != count:
    fail(f"user_error_count {got.get('user_error_count')} != {count}")
ok()
