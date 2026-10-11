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

if (WS / "test_stats.py").read_text() != (SETUP / "test_stats.py").read_text():
    fail("test_stats.py was modified")
r = subprocess.run([sys.executable, "test_stats.py"], cwd=WS, capture_output=True, text=True, timeout=60)
if "all tests passed" not in r.stdout:
    fail("tests still fail: " + (r.stderr or r.stdout)[-300:])
r = subprocess.run([sys.executable, "-c", "from stats import moving_average as m; assert m([1,2,3],1)==[1,2,3]; assert m([1,2,3,4,5],5)==[3.0]"],
                   cwd=WS, capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    fail("hidden check failed: " + r.stderr[-300:])
ok()
