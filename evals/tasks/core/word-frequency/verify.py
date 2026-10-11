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

text = (SETUP / "essay.txt").read_text().lower()
stop = set((SETUP / "stopwords.txt").read_text().split())
freq = {}
for w in re.findall(r"[a-z]+", text):
    if w not in stop:
        freq[w] = freq.get(w, 0) + 1
want = [f"{w} {c}" for w, c in sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:5]]
p = WS / "top5.txt"
if not p.exists():
    fail("top5.txt missing")
got = [" ".join(l.split()) for l in p.read_text().splitlines() if l.strip()]
if got != want:
    fail(f"got {got} want {want}")
ok()
