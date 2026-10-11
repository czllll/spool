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

if (WS / "test_calc.py").read_text() != (SETUP / "test_calc.py").read_text():
    fail("test_calc.py was modified")
r = subprocess.run([sys.executable, "test_calc.py"], cwd=WS, capture_output=True, text=True, timeout=60)
if "all 10 tests passed" not in r.stdout:
    fail("tests fail: " + (r.stdout + r.stderr)[-400:])
r = subprocess.run([sys.executable, "-c", "from calc import calculate as c; assert c('100 / 8') == 12.5; assert c('8 - 2 - 1') == 5; assert c('-(2 + 3)') == -5"],
                   cwd=WS, capture_output=True, text=True, timeout=60)
if r.returncode != 0:
    fail("hidden checks failed: " + r.stderr[-300:])
ok()
