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

import csv
expected = {}
with open(SETUP / "sales.csv") as fh:
    for row in csv.DictReader(fh):
        expected[row["region"]] = expected.get(row["region"], 0) + int(row["units"]) * float(row["unit_price"])
p = WS / "summary.json"
if not p.exists():
    fail("summary.json missing")
try:
    got = json.loads(p.read_text())
except Exception as exc:
    fail(f"summary.json is not valid JSON: {exc}")
if sorted(got) != sorted(expected):
    fail(f"regions {sorted(got)} != {sorted(expected)}")
for region, total in expected.items():
    if abs(float(got[region]) - total) > 0.011:
        fail(f"{region}: {got[region]} != {total:.2f}")
ok()
