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

import ast
want = []
for p in sorted((SETUP / "src").rglob("*.py")):
    src = p.read_text()
    lines = src.splitlines()
    for node in ast.parse(src).body:
        if isinstance(node, ast.FunctionDef):
            seg = lines[node.lineno - 1: node.end_lineno]
            if any("TODO" in l for l in seg):
                want.append(f"{p.relative_to(SETUP).as_posix()}:{node.name}")
want.sort()
p = WS / "todo_report.md"
if not p.exists():
    fail("todo_report.md missing")
got = [l.strip().strip("`").lstrip("-* ").strip().strip("`") for l in p.read_text().splitlines() if ":" in l and ".py" in l]
if sorted(got) != want:
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    fail(f"missing {missing[:4]} extra {extra[:4]}")
ok()
