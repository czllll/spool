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

for p in WS.rglob("*.py"):
    if ".spool" in p.parts:
        continue
    if "get_total" in p.read_text():
        fail(f"get_total still used in {p.relative_to(WS)}")
if "def compute_total" not in (WS / "shop" / "cart.py").read_text():
    fail("compute_total is not defined in shop/cart.py")
r = subprocess.run([sys.executable, "tests/test_shop.py"], cwd=WS, capture_output=True, text=True, timeout=60)
if "all tests passed" not in r.stdout:
    fail("tests fail: " + (r.stderr or r.stdout)[-300:])
ok()
