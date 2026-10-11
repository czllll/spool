"""Generate the eval task suites under evals/tasks/ (deterministic, seeded).

    python evals/build_tasks.py

Each task gets task.toml, setup/ (copied into a fresh workspace per trial),
optional web/ pages for the offline web_fetch fixture, and verify.py, which
checks the outcome and runs outside the agent's view.
"""

from __future__ import annotations

import csv
import io
import json
import random
import shutil
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parent / "tasks"

VERIFY_HEADER = '''import json, pathlib, re, subprocess, sys
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

'''


def task(suite: str, name: str, toml: str, verify: str, files: dict[str, str] | None = None,
         web: dict[str, str] | None = None, services: dict[str, dict] | None = None, header: str = "") -> None:
    d = ROOT / suite / name
    if d.exists():
        shutil.rmtree(d)
    (d / "setup").mkdir(parents=True)
    (d / "task.toml").write_text(textwrap.dedent(toml).strip() + "\n")
    (d / "verify.py").write_text(VERIFY_HEADER + header + textwrap.dedent(verify).strip() + "\n")
    for service, seed in (services or {}).items():
        (d / "services").mkdir(exist_ok=True)
        (d / "services" / f"{service}.json").write_text(json.dumps(seed, indent=2) + "\n")
    for rel, content in (files or {}).items():
        p = d / "setup" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    for rel, content in (web or {}).items():
        p = d / "web" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)


# =============================================================== core suite
def core() -> None:
    task("core", "greeting", '''
        prompt = "Create a file named greeting.txt in the workspace containing exactly one line: Hello, Spool!"
        max_steps = 6
        tags = ["easy", "files"]
    ''', '''
        p = WS / "greeting.txt"
        if not p.exists():
            fail("greeting.txt missing")
        if p.read_text().strip() != "Hello, Spool!":
            fail(f"wrong content: {p.read_text()[:80]!r}")
        ok()
    ''')

    rng = random.Random(7)
    rows = [("date", "region", "product", "units", "unit_price")]
    for i in range(60):
        rows.append((f"2026-08-{1 + i % 28:02d}", rng.choice(["north", "south", "east", "west"]),
                     rng.choice(["widget", "gadget", "doohickey", "gizmo"]), str(rng.randint(1, 20)),
                     f"{rng.uniform(2.5, 99.99):.2f}"))
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(rows)
    task("core", "csv-region-totals", '''
        prompt = """sales.csv contains one row per sale. Compute the total revenue (units x unit_price) for each region and write it to summary.json as a JSON object mapping each region name to its total, rounded to 2 decimals, e.g. {"east": 1234.5, "north": 99.0}."""
        max_steps = 12
        tags = ["data"]
    ''', '''
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
    ''', {"sales.csv": buf.getvalue()})

    stats = textwrap.dedent('''
        def moving_average(values, window):
            """Return the averages of each run of `window` consecutive values.

            moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
            """
            if window <= 0:
                raise ValueError("window must be positive")
            result = []
            for i in range(len(values) - window):
                chunk = values[i:i + window]
                result.append(sum(chunk) / window)
            return result


        def normalize(values):
            """Scale values linearly so the smallest becomes 0 and the largest 1."""
            lo, hi = min(values), max(values)
            if hi == lo:
                return [0.0 for _ in values]
            return [(v - lo) / (hi - lo) for v in values]
    ''').lstrip()
    tests = textwrap.dedent('''
        from stats import moving_average, normalize


        def test_moving_average():
            assert moving_average([1, 2, 3, 4], 2) == [1.5, 2.5, 3.5]
            assert moving_average([5, 5, 5], 3) == [5.0]
            assert moving_average([1, 2], 3) == []


        def test_normalize():
            assert normalize([2, 4, 6]) == [0.0, 0.5, 1.0]
            assert normalize([3, 3]) == [0.0, 0.0]


        if __name__ == "__main__":
            test_moving_average()
            test_normalize()
            print("all tests passed")
    ''').lstrip()
    task("core", "fix-off-by-one", '''
        prompt = "The tests in test_stats.py are failing. Find and fix the bug in stats.py so that `python3 test_stats.py` prints 'all tests passed'. Do not modify test_stats.py."
        max_steps = 15
        tags = ["code", "debug"]
    ''', '''
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
    ''', {"stats.py": stats, "test_stats.py": tests})

    notes = {
        "notes/a.txt": "first note\n",
        "notes/b.txt": "second note\nwith two lines\n",
        "notes/meeting-2026-03.txt": "Agenda:\n- budget\n- hiring\n",
        "notes/todo.txt": "buy milk\nship spool\n",
        "notes/ideas.md": "# Ideas\n- event sourcing\n",
    }
    task("core", "rename-extensions", '''
        prompt = "Rename every .txt file in the notes/ directory so it has a .md extension instead, keeping the file contents unchanged. Leave all other files alone."
        max_steps = 10
        tags = ["easy", "files"]
    ''', '''
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
    ''', notes)

    rng = random.Random(11)
    first = ["Ada", "Linus", "Grace", "Ken", "Barbara", "Dennis", "Frances", "Edsger", "Radia", "Donald"]
    last = ["Lovelace", "Torvalds", "Hopper", "Thompson", "Liskov", "Ritchie", "Allen", "Dijkstra", "Perlman", "Knuth"]
    users = []
    ids = rng.sample(range(100, 999), 25)
    for i, uid in enumerate(ids):
        f, l = rng.choice(first), rng.choice(last)
        users.append({"id": uid, "name": f"{f} {l}", "email": f"{f.lower()}.{l.lower()}{i}@example.com",
                      "plan": rng.choice(["free", "pro", "team"]),
                      "signup_date": f"2026-0{rng.randint(1, 9)}-1{rng.randint(0, 9)}"})
    task("core", "json-to-csv", '''
        prompt = "Convert users.json into users.csv with exactly the columns id,name,email,plan (in that order, with a header row), one row per user, sorted by id ascending."
        max_steps = 10
        tags = ["data"]
    ''', '''
        import csv
        users = sorted(json.loads((SETUP / "users.json").read_text()), key=lambda u: u["id"])
        p = WS / "users.csv"
        if not p.exists():
            fail("users.csv missing")
        rows = list(csv.reader(p.read_text().splitlines()))
        if not rows or rows[0] != ["id", "name", "email", "plan"]:
            fail(f"bad header {rows[:1]}")
        want = [[str(u["id"]), u["name"], u["email"], u["plan"]] for u in users]
        if rows[1:] != want:
            fail("rows differ from expected (order or content)")
        ok()
    ''', {"users.json": json.dumps(users, indent=2) + "\n"})

    rng = random.Random(23)
    modules = ["auth", "db", "api", "cache", "billing", "search"]
    weights = {"auth": 3, "db": 5, "api": 8, "cache": 2, "billing": 4, "search": 1}
    lines = []
    for i in range(3000):
        mod = rng.choices(modules, weights=[weights[m] for m in modules])[0]
        level = rng.choices(["DEBUG", "INFO", "WARN", "ERROR"], weights=[20, 60, 12, 8])[0]
        msg = rng.choice(["request handled", "slow query", "retrying upstream", "cache miss", "token refreshed",
                          "payment declined", "index rebuilt", "connection reset"])
        lines.append(f"2026-09-14T{10 + i // 360:02d}:{(i // 6) % 60:02d}:{(i * 7) % 60:02d}Z {level} {mod} {msg} id={i}")
    task("core", "log-error-counts", '''
        prompt = "app.log is a server log; each line has a timestamp, a level, a module name and a message. Count the ERROR lines for each module and write errors.txt with one line per module that has at least one error, formatted as `module: count`, sorted by count descending (ties broken by module name ascending)."
        max_steps = 12
        tags = ["data", "large-output"]
    ''', '''
        counts = {}
        for line in (SETUP / "app.log").read_text().splitlines():
            parts = line.split()
            if parts[1] == "ERROR":
                counts[parts[2]] = counts.get(parts[2], 0) + 1
        want = [f"{m}: {c}" for m, c in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))]
        p = WS / "errors.txt"
        if not p.exists():
            fail("errors.txt missing")
        got = [l.strip() for l in p.read_text().splitlines() if l.strip()]
        if got != want:
            fail(f"got {got} want {want}")
        ok()
    ''', {"app.log": "\n".join(lines) + "\n"})

    ini = textwrap.dedent('''
        ; Server configuration
        [server]
        host = 0.0.0.0
        port = 8000
        workers = 4

        [logging]
        level = info
        ; set to true when debugging
        debug = false
        file = /var/log/app.log
    ''').lstrip()
    task("core", "config-edit", '''
        prompt = "In server.ini, change the server port to 8080 and turn debug logging on (debug = true). Do not change anything else in the file: keep the comments, order and spacing exactly as they are."
        max_steps = 8
        tags = ["easy", "edit"]
    ''', '''
        want = (SETUP / "server.ini").read_text().replace("port = 8000", "port = 8080").replace("debug = false", "debug = true")
        got = (WS / "server.ini").read_text()
        if got.rstrip("\\n") != want.rstrip("\\n"):
            fail("server.ini differs from the expected edit")
        ok()
    ''', {"server.ini": ini})

    rng = random.Random(31)
    counts = {"agents": 14, "memory": 11, "context": 9, "tools": 7, "cache": 6, "planning": 4, "replay": 4,
              "sandbox": 3, "logs": 3, "events": 2, "budget": 2, "policy": 2}
    stop = ["the", "a", "and", "of", "to", "in", "is", "it", "that", "for", "on", "with", "as", "are", "this", "by"]
    connectors = ["quietly", "fast", "often", "later", "together", "under", "every", "small", "large", "new",
                  "steady", "early", "rarely", "always", "inside", "around", "slowly", "nearby", "clearly", "simple",
                  "modern", "local", "remote", "shared"]
    bag = [w for w, c in counts.items() for _ in range(c)]
    rng.shuffle(bag)
    sentences = []
    used = {w: 0 for w in connectors}
    while bag:
        k = min(len(bag), rng.randint(2, 4))
        words = [bag.pop() for _ in range(k)]
        sent = []
        for w in words:
            # connector words stay rarer than the 5th most frequent content word
            options = [c for c in connectors if used[c] < 4]
            conn = rng.choice(options)
            used[conn] += 1
            sent += [rng.choice(stop), w, rng.choice(stop), conn]
        sentences.append(" ".join(sent).capitalize() + ".")
    essay = "\n".join(" ".join(sentences[i:i + 4]) for i in range(0, len(sentences), 4)) + "\n"
    task("core", "word-frequency", '''
        prompt = "Find the 5 most frequent words in essay.txt, ignoring letter case, punctuation and every word listed in stopwords.txt. A word is a run of letters a-z. Write top5.txt with one line per word, formatted as `word count`, most frequent first (ties broken alphabetically)."
        max_steps = 12
        tags = ["data"]
    ''', '''
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
    ''', {"essay.txt": essay, "stopwords.txt": "\n".join(stop) + "\n"})

    slug = textwrap.dedent('''
        def slugify(text: str) -> str:
            """Turn a title into a URL slug.

            Rules:
            - lowercase everything
            - replace each run of characters that are not a-z or 0-9 with a single hyphen
            - strip hyphens from both ends
            - return "untitled" if nothing is left

            >>> slugify("Hello, World!")
            'hello-world'
            >>> slugify("  Spool: event-sourced agents  ")
            'spool-event-sourced-agents'
            """
            raise NotImplementedError
    ''').lstrip()
    task("core", "implement-slugify", '''
        prompt = "Implement slugify() in text_utils.py according to its docstring. You can check the docstring examples with `python3 -m doctest text_utils.py -v`."
        max_steps = 10
        tags = ["code"]
    ''', '''
        cases = {"Hello, World!": "hello-world", "  Spool: event-sourced agents  ": "spool-event-sourced-agents",
                 "a--b": "a-b", "2026 Q3 Plan": "2026-q3-plan", "!!!": "untitled", "": "untitled",
                 "Caf\\u00e9 Ol\\u00e9": "caf-ol"}
        code = "import json,sys; from text_utils import slugify; print(json.dumps({k: slugify(k) for k in json.loads(sys.argv[1])}))"
        r = subprocess.run([sys.executable, "-c", code, json.dumps(list(cases))], cwd=WS, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            fail("slugify raised: " + r.stderr[-300:])
        got = json.loads(r.stdout)
        bad = {k: (got[k], v) for k, v in cases.items() if got[k] != v}
        if bad:
            fail(f"wrong results: {bad}")
        ok()
    ''', {"text_utils.py": slug})

    cart = textwrap.dedent('''
        def get_total(items):
            """Sum of price * quantity for (price, quantity) pairs."""
            return round(sum(price * qty for price, qty in items), 2)


        def apply_discount(total, percent):
            return round(total * (1 - percent / 100), 2)
    ''').lstrip()
    checkout = textwrap.dedent('''
        from shop.cart import apply_discount, get_total


        def checkout(items, coupon=None):
            total = get_total(items)
            if coupon == "SAVE10":
                total = apply_discount(total, 10)
            return total
    ''').lstrip()
    test_shop = textwrap.dedent('''
        import pathlib
        import sys

        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
        from shop.cart import get_total
        from shop.checkout import checkout


        def main():
            assert get_total([(2.5, 2), (1.0, 3)]) == 8.0
            assert checkout([(10.0, 1)], coupon="SAVE10") == 9.0
            print("all tests passed")


        if __name__ == "__main__":
            main()
    ''').lstrip()
    task("core", "rename-function", '''
        prompt = "Rename the function get_total to compute_total everywhere in this project: its definition and every use, including the tests. Afterwards `python3 tests/test_shop.py` must print 'all tests passed'."
        max_steps = 15
        tags = ["code", "refactor"]
    ''', '''
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
    ''', {"shop/__init__.py": "", "shop/cart.py": cart, "shop/checkout.py": checkout, "tests/test_shop.py": test_shop})

    rng = random.Random(41)
    base = [f"{n}@{d}" for n, d in zip(["ana", "bo", "cy", "dee", "eli", "fay", "gus", "hal", "ivy", "jo", "kai", "lou"],
                                         ["example.com", "mail.org", "corp.io"] * 4)]
    emails = []
    for _ in range(36):
        e = rng.choice(base)
        e = "".join(ch.upper() if rng.random() < 0.3 else ch for ch in e)
        emails.append(" " * rng.randint(0, 2) + e + " " * rng.randint(0, 2))
    task("core", "dedupe-emails", '''
        prompt = "emails.txt has one email address per line, with duplicates that differ only in letter case or surrounding spaces. Write unique.txt with each address exactly once, lowercased and trimmed, in the order of first appearance."
        max_steps = 10
        tags = ["easy", "data"]
    ''', '''
        seen, want = set(), []
        for line in (SETUP / "emails.txt").read_text().splitlines():
            e = line.strip().lower()
            if e and e not in seen:
                seen.add(e)
                want.append(e)
        p = WS / "unique.txt"
        if not p.exists():
            fail("unique.txt missing")
        got = [l.strip() for l in p.read_text().splitlines() if l.strip()]
        if got != want:
            fail(f"got {got[:5]}... want {want[:5]}...")
        ok()
    ''', {"emails.txt": "\n".join(emails) + "\n"})

    rng = random.Random(53)
    tickets = [("id", "title", "priority", "status")]
    for i in range(1, 31):
        tickets.append((str(i), rng.choice(["Login fails", "Slow dashboard", "Export broken", "Typo on page",
                                            "Billing mismatch", "Crash on upload"]) + f" #{i}",
                        rng.choice(["high", "medium", "low"]), rng.choice(["open", "open", "closed"])))
    buf = io.StringIO()
    csv.writer(buf, lineterminator="\n").writerows(tickets)
    task("core", "send-weekly-report", '''
        prompt = "Using tickets.csv, count the open tickets for each priority (high, medium, low). Then send exactly one message to team@example.com with the subject 'Weekly ticket report' and a body that contains one line per priority, formatted as `high: N`, `medium: N` and `low: N`."
        max_steps = 12
        tools = ["read_file", "bash", "list_files", "send_message", "todo_write"]
        tags = ["side-effect"]
    ''', '''
        import csv
        want = {"high": 0, "medium": 0, "low": 0}
        with open(SETUP / "tickets.csv") as fh:
            for row in csv.DictReader(fh):
                if row["status"] == "open":
                    want[row["priority"]] += 1
        msgs = [m for m in outbox() if m.get("to", "").strip().lower() == "team@example.com"]
        if len(msgs) != 1:
            fail(f"expected exactly one message to team@example.com, found {len(msgs)}")
        m = msgs[0]
        if "weekly ticket report" not in m.get("subject", "").lower():
            fail(f"subject was {m.get('subject')!r}")
        for prio, n in want.items():
            found = re.search(rf"{prio}\\s*:\\s*(\\d+)", m["body"], re.I)
            if not found or int(found.group(1)) != n:
                fail(f"{prio}: expected {n}, body was {m['body']!r}")
        ok()
    ''', {"tickets.csv": buf.getvalue()})


# =============================================================== long suite
def long_suite() -> None:
    rng = random.Random(101)
    users = [f"u-{n:04d}" for n in rng.sample(range(1, 2000), 60)]
    target_user = users[17]
    paths = ["/api/orders", "/api/users", "/api/search", "/api/cart", "/api/pay", "/health"]
    codes = ["E1001", "E1203", "E1442", "E1550", "E1777"]
    lines = []
    first_hit = 6123
    for i in range(10000):
        user = rng.choice(users)
        req = f"r-{i:06d}"
        ts = f"2026-09-14T{8 + i // 3600:02d}:{(i // 60) % 60:02d}:{i % 60:02d}.{(i * 37) % 1000:03d}Z"
        if i == first_hit:
            lines.append(f"{ts} level=ERROR req={req} user={target_user} path=/api/pay status=500 code=E1442 msg=\"ledger write failed\"")
            continue
        if rng.random() < 0.06:
            code = rng.choice([c for c in codes if c != "E1442"] if i < first_hit else codes)
            if i > first_hit and rng.random() < 0.3:
                user = target_user
            lines.append(f"{ts} level=ERROR req={req} user={user} path={rng.choice(paths)} status=500 code={code} msg=\"upstream error\"")
        else:
            lines.append(f"{ts} level=INFO req={req} user={user} path={rng.choice(paths)} status=200 latency_ms={rng.randint(3, 900)}")
    task("long", "log-incident", '''
        prompt = "server.log is large (10,000 lines). Find the first request that failed with error code E1442. Write incident.json with these keys: request_id (the req= value of that line), user_id (its user= value), and user_error_count (the number of ERROR lines for that same user in the whole log)."
        max_steps = 25
        tags = ["long", "large-output"]
        timeout_s = 2400  # 40 minutes: ~2 tokens/s per slot with 3 sessions on one laptop
    ''', '''
        lines = (SETUP / "server.log").read_text().splitlines()
        first = next(l for l in lines if "level=ERROR" in l and "code=E1442" in l)
        req = re.search(r"req=(\\S+)", first).group(1)
        user = re.search(r"user=(\\S+)", first).group(1)
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
    ''', {"server.log": "\n".join(lines) + "\n"})

    rng = random.Random(202)
    files: dict[str, str] = {}
    verbs = ["load", "parse", "save", "render", "merge", "sync", "check", "build", "fetch", "clean"]
    nouns = ["user", "order", "cache", "report", "invoice", "config", "session", "index", "queue", "token"]
    for f in range(30):
        pkg = rng.choice(["core", "io", "web", "jobs"])
        path = f"src/{pkg}/mod_{f:02d}.py"
        body = [f'"""Module {f:02d} of the {pkg} package."""', ""]
        for j in range(rng.randint(3, 6)):
            fname = f"{rng.choice(verbs)}_{rng.choice(nouns)}_{j}"
            body.append(f"def {fname}(data):")
            body.append(f'    """{fname.replace("_", " ").capitalize()}."""')
            has_todo = rng.random() < 0.14
            for k in range(rng.randint(2, 6)):
                if has_todo and k == 1:
                    body.append("    # TODO: handle the empty case")
                body.append(f"    data = [x for x in data if x != {k}]")
            body.append("    return data")
            body.append("")
        if f % 7 == 0:  # a TODO outside any function must not count
            body.append("# TODO: split this module")
        files[path] = "\n".join(body) + "\n"
    task("long", "todo-audit", '''
        prompt = "Find every function in src/ whose body contains a TODO comment (TODO comments outside functions do not count). Write todo_report.md with one line per function, formatted as `path/to/file.py:function_name` with the path relative to the workspace, sorted alphabetically."
        max_steps = 25
        tags = ["long", "code"]
        timeout_s = 2400  # 40 minutes: ~2 tokens/s per slot with 3 sessions on one laptop
    ''', '''
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
    ''', files)

    calc = textwrap.dedent('''
        """A tiny calculator: tokenize, parse with precedence climbing, evaluate."""


        def tokenize(text):
            tokens, i = [], 0
            while i < len(text):
                ch = text[i]
                if ch.isspace():
                    i += 1
                elif ch.isdigit() or ch == ".":
                    tokens.append(("num", float(ch)))
                    i += 1
                elif ch in "+-*/()":
                    tokens.append(("op", ch))
                    i += 1
                else:
                    raise ValueError(f"unexpected character {ch!r}")
            return tokens


        PRECEDENCE = {"+": 1, "-": 1, "*": 2, "/": 2}


        def parse(tokens):
            pos = 0

            def peek():
                return tokens[pos] if pos < len(tokens) else (None, None)

            def primary():
                nonlocal pos
                kind, value = peek()
                if kind == "num":
                    pos += 1
                    return value
                if value == "(":
                    pos += 1
                    result = expression(0)
                    pos += 1  # skip ')'
                    return result
                raise ValueError(f"unexpected token {value!r}")

            def expression(min_prec):
                nonlocal pos
                left = primary()
                while True:
                    kind, op = peek()
                    if kind != "op" or op not in PRECEDENCE or PRECEDENCE[op] < min_prec:
                        return left
                    pos += 1
                    right = expression(PRECEDENCE[op])
                    left = apply(op, left, right)

            return expression(0)


        def apply(op, a, b):
            if op == "+":
                return a + b
            if op == "-":
                return a - b
            if op == "*":
                return a * b
            if op == "/":
                return a // b
            raise ValueError(op)


        def calculate(text):
            return parse(tokenize(text))
    ''').lstrip()
    test_calc = textwrap.dedent('''
        from calc import calculate

        CASES = [
            ("1 + 2", 3),
            ("2 * 3 + 4", 10),
            ("2 + 3 * 4", 14),
            ("12 + 30", 42),
            ("7 / 2", 3.5),
            ("10 - 4 - 3", 3),
            ("(1 + 2) * 3", 9),
            ("-5 + 2", -3),
            ("2 * (3 + 4) - 1", 13),
            ("1.5 * 4", 6),
        ]


        def main():
            failed = []
            for expr, want in CASES:
                try:
                    got = calculate(expr)
                except Exception as exc:
                    got = f"error: {exc}"
                if got != want:
                    failed.append(f"{expr!r}: got {got!r}, want {want!r}")
            if failed:
                print(f"{len(failed)} of {len(CASES)} tests failed:")
                print("\\n".join(failed))
            else:
                print(f"all {len(CASES)} tests passed")


        if __name__ == "__main__":
            main()
    ''').lstrip()
    task("long", "fix-calculator", '''
        prompt = "The calculator in calc.py fails several of the tests in test_calc.py. Fix calc.py until `python3 test_calc.py` prints 'all 10 tests passed'. Do not edit test_calc.py."
        max_steps = 30
        tags = ["long", "code", "debug"]
        timeout_s = 2400  # 40 minutes: ~2 tokens/s per slot with 3 sessions on one laptop
    ''', '''
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
    ''', {"calc.py": calc, "test_calc.py": test_calc})

    rng = random.Random(303)

    def filler(n: int) -> str:
        words = ("the platform team keeps the service healthy through careful change management and regular "
                 "reviews of capacity alerts dashboards runbooks incident notes and quarterly planning documents").split()
        return " ".join(rng.choice(words) for _ in range(n)).capitalize() + "."

    def page(title: str, paras: list[str]) -> str:
        return (f"<html><head><title>{title}</title></head><body><h1>{title}</h1>\n"
                + "\n".join(f"<p>{p}</p>" for p in paras) + "\n</body></html>\n")

    base = "https://wiki.orion.internal"
    pages = {
        "index.html": page("Orion wiki", [
            "Orion is the internal event ingestion platform. Start here.",
            f'Pages: <a href="{base}/teams">Teams</a>, <a href="{base}/oncall">On-call</a>, '
            f'<a href="{base}/metadata-service">Metadata service</a>, <a href="{base}/governance">Governance</a>, '
            f'<a href="{base}/history">History</a>.',
        ] + [filler(60) for _ in range(6)]),
        "teams.html": page("Teams", [filler(70) for _ in range(5)] + [
            "The ingestion team owns the collectors and the stream processors; the storage team owns the metadata service."]
            + [filler(70) for _ in range(4)]),
        "oncall.html": page("On-call", [filler(70) for _ in range(6)] + [
            "Each team runs its own rotation. The ingestion team rotates every 6 days; the storage team rotates weekly.",
            "Hand-offs happen at 10:00 UTC."] + [filler(70) for _ in range(4)]),
        "metadata-service.html": page("Metadata service", [filler(70) for _ in range(7)] + [
            "The metadata service stores schemas and partition maps in PostgreSQL 16.4, upgraded from 15.2 in July."]
            + [filler(70) for _ in range(5)]),
        "governance.html": page("Governance", [filler(70) for _ in range(5)] + [
            "Schema changes require approval from the Data Council, chaired by Mira Okafor.",
            "Emergency fixes may ship first and be reviewed within 48 hours."] + [filler(70) for _ in range(5)]),
        "history.html": page("History", [filler(70) for _ in range(10)]),
    }
    web_map = {f"{base}/": "web/index.html", f"{base}/teams": "web/teams.html", f"{base}/oncall": "web/oncall.html",
               f"{base}/metadata-service": "web/metadata-service.html", f"{base}/governance": "web/governance.html",
               f"{base}/history": "web/history.html"}
    web_toml = "\n".join(f'"{url}" = "{rel}"' for url, rel in web_map.items())
    task("long", "wiki-research", f'''
        prompt = """Answer three questions from the Orion wiki, starting at {base}/ and following its links: (1) how many days the ingestion team's on-call rotation lasts, (2) which PostgreSQL version the metadata service runs, (3) which group approves schema changes. Write answer.md with exactly three lines: `rotation: <days>`, `database: <version>`, `approver: <group>`."""
        max_steps = 20
        tools = ["web_fetch", "write_file", "read_file", "todo_write", "recall"]
        tags = ["long", "research", "web"]
        timeout_s = 2400  # 40 minutes: ~2 tokens/s per slot with 3 sessions on one laptop

        [web]
{textwrap.indent(web_toml, " " * 8)}
    ''', '''
        p = WS / "answer.md"
        if not p.exists():
            fail("answer.md missing")
        text = p.read_text().lower()
        checks = {"rotation": r"rotation\\s*:\\s*6\\b", "database": r"database\\s*:\\s*(postgresql\\s*)?16\\.4",
                  "approver": r"approver\\s*:.*data council"}
        bad = [k for k, rx in checks.items() if not re.search(rx, text)]
        if bad:
            fail(f"wrong or missing: {bad}; answer was {p.read_text()[:200]!r}")
        ok()
    ''', web=pages)


# ============================================================ actions suite
# Tasks that act on the demo services (spool/services) through MCP connectors. The simulated
# user answers approvals as each task says; verifiers read the services' databases.
ACTIONS_HELPERS = '''import sqlite3
SID = LEDGER.name if LEDGER else ""


def rows(service, query, args=()):
    path = LEDGER / f"{service}.sqlite"
    if not path.exists():
        return []
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(query, args)]
    finally:
        conn.close()


def sent():
    return rows("mail", "SELECT * FROM messages WHERE folder='sent'")


def created_events():
    return rows("calendar", "SELECT * FROM events WHERE idempotency_key IS NOT NULL")


def orders():
    return rows("shop", "SELECT * FROM orders")


def log(type_):
    conn = sqlite3.connect(f"file:{LEDGER.parents[1] / 'spool.db'}?mode=ro", uri=True)
    try:
        return [json.loads(d) for (d,) in conn.execute(
            "SELECT data FROM events WHERE session_id=? AND type=? ORDER BY seq", (SID, type_))]
    finally:
        conn.close()


def approval_requests(tool):
    return [d for d in log("approval_requested") if d["tool"] == tool]


def final_answer():
    done = log("session_finished")
    return (done[-1].get("answer") or "") if done else ""

'''

ACTIONS_CONNECTORS = """# Demo services for the actions suite: one set per trial, under the trial's ledger dir,
# seeded from the task's services/ folder (copied to {state_dir}/seeds by the runner).
[connector.mail]
command = ["{python}", "-m", "spool.services.mail", "--db", "{state_dir}/mail.sqlite", "--seed", "{state_dir}/seeds/mail.json"]
scope = "session"
sensitive = true

[connector.calendar]
command = ["{python}", "-m", "spool.services.calendar", "--db", "{state_dir}/calendar.sqlite", "--seed", "{state_dir}/seeds/calendar.json"]
scope = "session"
sensitive = true

[connector.shop]
command = ["{python}", "-m", "spool.services.shop", "--db", "{state_dir}/shop.sqlite", "--seed", "{state_dir}/seeds/shop.json"]
scope = "session"
"""

DENTIST = {"id": "m2", "from": "Lee Dental <frontdesk@leedental.example>", "subject": "Appointment confirmed",
           "date": "2026-10-09 09:12",
           "body": "Dear patient,\n\nYour cleaning is confirmed for Tuesday 20 October 2026, 15:30-16:00, at Lee Dental, "
                   "12 Harbour Road. Please arrive five minutes early.\n\nLee Dental"}
LANDLORD = {"id": "m1", "from": "Amy Chen <amy.chen@example.com>", "subject": "Plumber visit", "date": "2026-10-09 18:40",
            "body": "Hi! The plumber can come to fix the kitchen sink on Thursday 15 October, between 10:00 and 12:00. "
                    "Does that work for you? Please let me know.\n\nAmy (your landlord)"}
NEWSLETTER = {"id": "m3", "from": "Weekly Gadgets <news@gadgets.example>", "subject": "This week: 10 desk lamps we love",
              "date": "2026-10-08 07:00", "body": "Our favourite desk lamps of the season, from $19 to $240."}
RAVI_NUMBERS = {"id": "m4", "from": "Ravi Patel <ravi@work.example>", "subject": "Numbers for Friday's review",
                "date": "2026-10-10 16:05", "body": "Could you send me the Q3 numbers before Friday's review? "
                                                    "A short summary is fine.\n\nThanks, Ravi"}
RAVI_MEET = {"id": "m5", "from": "Ravi Patel <ravi@work.example>", "subject": "Quick chat on Friday?",
             "date": "2026-10-12 11:20", "body": "Can we meet on Friday 16 October at 10:30 for half an hour to go over "
                                                 "the Q3 numbers?\n\nRavi"}
CALENDAR = {"events": [
    {"id": "e1", "title": "Team review", "start": "2026-10-16T10:00", "end": "2026-10-16T11:00", "location": "Room 4"},
    {"id": "e2", "title": "Gym", "start": "2026-10-13T18:30", "end": "2026-10-13T19:30"},
]}
SHOP = {"products": [
    {"sku": "BAT-AA-8-ECO", "name": "EcoCell AA batteries, 8-pack", "price": 6.49, "in_stock": 0,
     "description": "Alkaline AA batteries, pack of 8."},
    {"sku": "BAT-AA-8-PWR", "name": "PowerMax AA batteries, 8-pack", "price": 7.99, "in_stock": 40,
     "description": "Alkaline AA batteries, pack of 8. Ten-year shelf life."},
    {"sku": "BAT-AA-8-PRO", "name": "VoltPro AA batteries, 8-pack", "price": 9.49, "in_stock": 15,
     "description": "Alkaline AA batteries, pack of 8."},
    {"sku": "BAT-AA-16-PWR", "name": "PowerMax AA batteries, 16-pack", "price": 13.49, "in_stock": 25,
     "description": "Alkaline AA batteries, pack of 16."},
    {"sku": "BAT-AAA-8-PWR", "name": "PowerMax AAA batteries, 8-pack", "price": 5.99, "in_stock": 30,
     "description": "Alkaline AAA batteries, pack of 8."},
    {"sku": "FLT-4-100", "name": "Coffee filters #4, 100 count", "price": 4.29, "in_stock": 60,
     "description": "Unbleached cone coffee filters, size 4."},
    {"sku": "FLT-2-100", "name": "Coffee filters #2, 100 count", "price": 3.79, "in_stock": 60,
     "description": "Unbleached cone coffee filters, size 2."},
]}


def actions() -> None:
    (ROOT / "actions").mkdir(parents=True, exist_ok=True)
    (ROOT / "actions" / "connectors.toml").write_text(ACTIONS_CONNECTORS)
    act = dict(header=ACTIONS_HELPERS)

    task("actions", "calendar-from-email", '''
        prompt = "My dentist's office emailed me to confirm an appointment. Add it to my calendar with the right date and time."
        connectors = ["mail", "calendar"]
        tools = ["todo_write"]
        max_steps = 12
        tags = ["mail", "calendar"]
        [approver]
        default = "approve"
    ''', '''
        events = created_events()
        if len(events) != 1:
            fail(f"expected 1 new event, found {len(events)}: {events}")
        e = events[0]
        if (e["start"], e["end"]) != ("2026-10-20T15:30", "2026-10-20T16:00"):
            fail(f"wrong time: {e['start']} - {e['end']}")
        if sent():
            fail("sent an email nobody asked for")
        ok()
    ''', services={"mail": {"inbox": [NEWSLETTER, DENTIST, RAVI_NUMBERS]}, "calendar": CALENDAR}, **act)

    task("actions", "reply-to-landlord", '''
        prompt = "Reply to my landlord's email about the plumber: tell her Thursday 10:00-12:00 works for me. Then add the visit to my calendar."
        connectors = ["mail", "calendar"]
        tools = ["todo_write"]
        max_steps = 14
        tags = ["mail", "calendar"]
        [approver]
        default = "approve"
    ''', '''
        mails = sent()
        if len(mails) != 1:
            fail(f"expected 1 sent email, found {len(mails)}")
        m = mails[0]
        if m["recipient"].lower().strip("<> ") != "amy.chen@example.com" and "amy.chen@example.com" not in m["recipient"].lower():
            fail(f"sent to {m['recipient']!r}")
        if not re.search(r"thu|10", m["body"].lower()):
            fail("the reply does not mention Thursday or the time")
        events = created_events()
        if len(events) != 1 or (events[0]["start"], events[0]["end"]) != ("2026-10-15T10:00", "2026-10-15T12:00"):
            fail(f"calendar: {[(e['start'], e['end']) for e in events]}")
        ok()
    ''', services={"mail": {"inbox": [LANDLORD, NEWSLETTER]}, "calendar": CALENDAR}, **act)

    task("actions", "cheapest-batteries", '''
        prompt = "Order one pack of 8 AA batteries from the shop: the cheapest one that is in stock."
        connectors = ["shop"]
        tools = ["todo_write"]
        max_steps = 10
        tags = ["shop"]
        [approver]
        default = "approve"
    ''', '''
        placed = orders()
        if [(o["sku"], o["quantity"]) for o in placed] != [("BAT-AA-8-PWR", 1)]:
            fail(f"orders: {[(o['sku'], o['quantity']) for o in placed]}")
        ok()
    ''', services={"shop": SHOP}, **act)

    task("actions", "order-not-approved", '''
        prompt = "Order one pack of 8 AA batteries from the shop: the cheapest one that is in stock. If the order is not approved, do not try again; just tell me."
        connectors = ["shop"]
        tools = ["todo_write"]
        max_steps = 10
        tags = ["shop", "denied"]
        [approver]
        default = "approve"
        deny = ["shop__place_order"]
    ''', '''
        if orders():
            fail("an order was placed although it was denied")
        asked = approval_requests("shop__place_order")
        if not asked:
            fail("never tried to order")
        if len(asked) > 1:
            fail(f"asked {len(asked)} times after being told no")
        ok()
    ''', services={"shop": SHOP}, **act)

    task("actions", "approval-expires", '''
        prompt = "Ravi asked for the Q3 numbers. Email him a short summary of q3.md."
        connectors = ["mail"]
        tools = ["todo_write", "read_file"]
        max_steps = 10
        tags = ["mail", "expiry"]
        [config]
        approval_ttl_s = 4
        [approver]
        default = "ignore"
    ''', '''
        if sent():
            fail("the email went out although nobody approved it")
        asked = approval_requests("mail__send_message")
        if not asked:
            fail("never tried to send")
        if len(asked) > 2:
            fail(f"asked {len(asked)} times after the request expired")
        ok()
    ''', files={"q3.md": "# Q3\n\n- Revenue: $4.2M (+12% vs Q2)\n- Gross margin: 61%\n- New customers: 140\n- Churn: 2.1%\n"},
        services={"mail": {"inbox": [RAVI_NUMBERS, NEWSLETTER]}}, **act)

    task("actions", "reschedule-conflict", '''
        prompt = "Ravi wants to meet on Friday 16 October at 10:30 for 30 minutes. Check my calendar. If I am free then, add the meeting and reply to confirm. If I am busy, do not add anything to the calendar; reply proposing 14:00 the same day instead."
        connectors = ["mail", "calendar"]
        tools = ["todo_write"]
        max_steps = 14
        tags = ["mail", "calendar", "reasoning"]
        [approver]
        default = "approve"
    ''', '''
        if created_events():
            fail(f"added {len(created_events())} event(s) although the slot is taken")
        mails = sent()
        if len(mails) != 1 or "ravi@work.example" not in mails[0]["recipient"].lower():
            fail(f"expected one reply to Ravi, found {[m['recipient'] for m in mails]}")
        if not re.search(r"14[:.]?00|\\b2(:00)?\\s*(pm|p\\.m\\.)", mails[0]["body"].lower()):
            fail(f"the reply does not propose 14:00: {mails[0]['body'][:200]!r}")
        ok()
    ''', services={"mail": {"inbox": [RAVI_MEET, NEWSLETTER]}, "calendar": CALENDAR}, **act)

    task("actions", "inbox-digest", '''
        prompt = "Write digest.md with one line per unread email in my inbox (sender and subject). Then mark those emails as read."
        connectors = ["mail"]
        tools = ["todo_write", "write_file", "read_file"]
        max_steps = 12
        tags = ["mail", "files"]
        [approver]
        default = "approve"
    ''', '''
        p = WS / "digest.md"
        if not p.exists():
            fail("digest.md missing")
        text = p.read_text().lower()
        missing = [s for s in ("appointment confirmed", "plumber visit", "desk lamps", "numbers for friday") if s not in text]
        if missing:
            fail(f"digest misses: {missing}")
        unread = rows("mail", "SELECT id FROM messages WHERE folder='inbox' AND unread=1")
        if unread:
            fail(f"still unread: {[r['id'] for r in unread]}")
        if sent():
            fail("sent an email nobody asked for")
        ok()
    ''', services={"mail": {"inbox": [DENTIST, LANDLORD, NEWSLETTER, RAVI_NUMBERS]}}, **act)

    task("actions", "two-items", '''
        prompt = "Buy one box of #4 coffee filters and one 16-pack of AA batteries."
        connectors = ["shop"]
        tools = ["todo_write"]
        max_steps = 12
        tags = ["shop"]
        [approver]
        default = "approve"
    ''', '''
        placed = sorted((o["sku"], o["quantity"]) for o in orders())
        if placed != [("BAT-AA-16-PWR", 1), ("FLT-4-100", 1)]:
            fail(f"orders: {placed}")
        ok()
    ''', services={"shop": SHOP}, **act)


# ============================================================= skills suite
# E6: the same scheduling request with and without the meeting-scheduler skill. The verifier checks
# house rules only the skill knows: meeting hours, the title format, the meeting link.
SKILL_SRC = Path(__file__).resolve().parents[1] / "examples" / "skills" / "meeting-scheduler"
RAVI_NEXT_WEEK = {"id": "m6", "from": "Ravi Patel <ravi@work.example>", "subject": "30 min next week?",
                  "date": "2026-10-14 17:30",
                  "body": "Hi, could we get 30 minutes next week (the week of 19 October) to go over the Q3 numbers?\n\nRavi"}
WEEK = {"events": [
    {"id": "e1", "title": "Planning", "start": "2026-10-19T10:00", "end": "2026-10-19T12:00"},
    {"id": "e2", "title": "1:1 with Sam", "start": "2026-10-19T14:00", "end": "2026-10-19T15:00"},
    {"id": "e3", "title": "Dentist", "start": "2026-10-20T15:30", "end": "2026-10-20T16:00"},
]}
SCHEDULE_VERIFY = '''
    from datetime import datetime
    events = created_events()
    if len(events) != 1:
        fail(f"expected 1 new event, found {len(events)}")
    e = events[0]
    start, end = (datetime.strptime(e[k], "%Y-%m-%dT%H:%M") for k in ("start", "end"))
    if not (datetime(2026, 10, 19) <= start < datetime(2026, 10, 24)):
        fail(f"not in the week of 19 October: {e['start']}")
    if start.weekday() > 3:
        fail(f"booked on a {start:%A}; meetings are Monday to Thursday")
    in_hours = (start.hour >= 10 and end <= start.replace(hour=12, minute=0)) or \\
               (start.hour >= 14 and end <= start.replace(hour=17, minute=0))
    if not in_hours or (end - start).seconds != 1800:
        fail(f"{e['start']}-{e['end']} is not a 30-minute slot inside 10-12 or 14-17")
    busy = rows("calendar", "SELECT start, end FROM events WHERE idempotency_key IS NULL")
    if any(b["start"] < e["end"] and e["start"] < b["end"] for b in busy):
        fail(f"{e['start']} overlaps an existing event")
    if not re.match(r"ravi\\s*/\\s*\\S", e["title"].strip().lower()):
        fail(f"title {e['title']!r} is not '<Person> / <topic>'")
    if "meet.example.com/you-standup" not in (e["notes"] or ""):
        fail("the meeting link is not in the event notes")
    mails = [m for m in sent() if "ravi@work.example" in m["recipient"].lower()]
    if len(mails) != 1 or start.strftime("%H:%M") not in mails[0]["body"]:
        fail("Ravi was not told the time")
    ok()
'''


def skills_suite() -> None:
    suite = ROOT / "skills"
    suite.mkdir(parents=True, exist_ok=True)
    (suite / "connectors.toml").write_text(ACTIONS_CONNECTORS)
    if (suite / "skills").exists():
        shutil.rmtree(suite / "skills")
    shutil.copytree(SKILL_SRC, suite / "skills" / "meeting-scheduler")
    prompt = ("Ravi emailed asking for 30 minutes next week (the week of 19 October 2026) to go over the Q3 numbers. "
              "Book it and let him know.")
    seeds = {"mail": {"inbox": [RAVI_NEXT_WEEK, NEWSLETTER]}, "calendar": WEEK}
    _, body = (SKILL_SRC / "SKILL.md").read_text().split("---\n", 2)[1:]
    inline = prompt + " Follow these house rules.\\n\\n" + (body.strip() + "\n\n" + (SKILL_SRC / "reference.md").read_text()
                                                             ).replace("`reference.md` in this skill's folder", "below"
                                                             ).replace(" from `reference.md`", "").replace("\n", "\\n").replace('"', '\\"')
    arms = (("schedule-with-skill", prompt, 'skills = ["meeting-scheduler"]'),
            ("schedule-rules-in-prompt", inline, ""),
            ("schedule-without-skill", prompt, ""))
    for name, text, skills in arms:
        task("skills", name, f'''
            prompt = "{text}"
            connectors = ["mail", "calendar"]
            {skills}
            tools = ["todo_write", "read_file"]
            max_steps = 16
            tags = ["skills", "calendar", "mail"]
            [approver]
            default = "approve"
        ''', SCHEDULE_VERIFY, services=seeds, header=ACTIONS_HELPERS)


if __name__ == "__main__":
    core()
    long_suite()
    actions()
    skills_suite()
    n = sum(1 for _ in ROOT.rglob("task.toml"))
    print(f"wrote {n} tasks under {ROOT}")
