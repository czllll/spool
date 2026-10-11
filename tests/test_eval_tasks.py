"""Every eval task must be solvable and its verifier must tell right from wrong.

For each task: the untouched workspace fails verification, and a reference
solution passes it.
"""

import ast
import csv
import io
import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from spool.eval.task import load_suite
from spool.tools.ledger import Ledger

ROOT = Path(__file__).resolve().parents[1] / "evals" / "tasks"
TASKS = {t.id: t for suite in ("core", "long") for t in load_suite(ROOT / suite)}

CALC_FIXED = '''
def tokenize(text):
    tokens, i = [], 0
    while i < len(text):
        ch = text[i]
        if ch.isspace():
            i += 1
        elif ch.isdigit() or ch == ".":
            j = i
            while j < len(text) and (text[j].isdigit() or text[j] == "."):
                j += 1
            tokens.append(("num", float(text[i:j])))
            i = j
        elif ch in "+-*/()":
            tokens.append(("op", ch))
            i += 1
        else:
            raise ValueError(ch)
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
        if value == "-":
            pos += 1
            return -primary()
        if value == "(":
            pos += 1
            result = expression(1)
            pos += 1
            return result
        raise ValueError(value)

    def expression(min_prec):
        nonlocal pos
        left = primary()
        while True:
            kind, op = peek()
            if kind != "op" or op not in PRECEDENCE or PRECEDENCE[op] < min_prec:
                return left
            pos += 1
            right = expression(PRECEDENCE[op] + 1)
            left = {"+": left + right, "-": left - right, "*": left * right, "/": left / right}[op]

    return expression(1)


def calculate(text):
    return parse(tokenize(text))
'''

SLUGIFY = '''
import re


def slugify(text):
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug or "untitled"
'''


def solve(task_id: str, ws: Path, ledger: Path, setup: Path) -> None:
    if task_id == "core/greeting":
        (ws / "greeting.txt").write_text("Hello, Spool!\n")
    elif task_id == "core/csv-region-totals":
        totals = {}
        for row in csv.DictReader((setup / "sales.csv").open()):
            totals[row["region"]] = totals.get(row["region"], 0) + int(row["units"]) * float(row["unit_price"])
        (ws / "summary.json").write_text(json.dumps({k: round(v, 2) for k, v in totals.items()}))
    elif task_id == "core/fix-off-by-one":
        p = ws / "stats.py"
        p.write_text(p.read_text().replace("range(len(values) - window)", "range(len(values) - window + 1)"))
    elif task_id == "core/rename-extensions":
        for p in (ws / "notes").glob("*.txt"):
            p.rename(p.with_suffix(".md"))
    elif task_id == "core/json-to-csv":
        users = sorted(json.loads((ws / "users.json").read_text()), key=lambda u: u["id"])
        buf = io.StringIO()
        w = csv.writer(buf, lineterminator="\n")
        w.writerow(["id", "name", "email", "plan"])
        w.writerows([[u["id"], u["name"], u["email"], u["plan"]] for u in users])
        (ws / "users.csv").write_text(buf.getvalue())
    elif task_id == "core/log-error-counts":
        counts = {}
        for line in (ws / "app.log").read_text().splitlines():
            parts = line.split()
            if parts[1] == "ERROR":
                counts[parts[2]] = counts.get(parts[2], 0) + 1
        rows = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
        (ws / "errors.txt").write_text("".join(f"{m}: {c}\n" for m, c in rows))
    elif task_id == "core/config-edit":
        p = ws / "server.ini"
        p.write_text(p.read_text().replace("port = 8000", "port = 8080").replace("debug = false", "debug = true"))
    elif task_id == "core/word-frequency":
        stop = set((ws / "stopwords.txt").read_text().split())
        freq = {}
        for w in re.findall(r"[a-z]+", (ws / "essay.txt").read_text().lower()):
            if w not in stop:
                freq[w] = freq.get(w, 0) + 1
        top = sorted(freq.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
        (ws / "top5.txt").write_text("".join(f"{w} {c}\n" for w, c in top))
    elif task_id == "core/implement-slugify":
        (ws / "text_utils.py").write_text(SLUGIFY)
    elif task_id == "core/rename-function":
        for p in ws.rglob("*.py"):
            p.write_text(p.read_text().replace("get_total", "compute_total"))
    elif task_id == "core/dedupe-emails":
        seen, out = set(), []
        for line in (ws / "emails.txt").read_text().splitlines():
            e = line.strip().lower()
            if e and e not in seen:
                seen.add(e)
                out.append(e)
        (ws / "unique.txt").write_text("\n".join(out) + "\n")
    elif task_id == "core/send-weekly-report":
        counts = {"high": 0, "medium": 0, "low": 0}
        for row in csv.DictReader((ws / "tickets.csv").open()):
            if row["status"] == "open":
                counts[row["priority"]] += 1
        body = "\n".join(f"{k}: {v}" for k, v in counts.items())
        Ledger(ledger / "outbox.jsonl").record("s:c1", {"to": "team@example.com", "subject": "Weekly ticket report", "body": body})
    elif task_id == "long/log-incident":
        lines = (ws / "server.log").read_text().splitlines()
        first = next(l for l in lines if "level=ERROR" in l and "code=E1442" in l)
        user = re.search(r"user=(\S+)", first).group(1)
        (ws / "incident.json").write_text(json.dumps({
            "request_id": re.search(r"req=(\S+)", first).group(1), "user_id": user,
            "user_error_count": sum(1 for l in lines if "level=ERROR" in l and f"user={user} " in l)}))
    elif task_id == "long/todo-audit":
        found = []
        for p in sorted((ws / "src").rglob("*.py")):
            src = p.read_text()
            lines = src.splitlines()
            for node in ast.parse(src).body:
                if isinstance(node, ast.FunctionDef) and any("TODO" in l for l in lines[node.lineno - 1: node.end_lineno]):
                    found.append(f"{p.relative_to(ws).as_posix()}:{node.name}")
        (ws / "todo_report.md").write_text("\n".join(sorted(found)) + "\n")
    elif task_id == "long/fix-calculator":
        (ws / "calc.py").write_text(CALC_FIXED)
    elif task_id == "long/wiki-research":
        (ws / "answer.md").write_text("rotation: 6\ndatabase: PostgreSQL 16.4\napprover: Data Council\n")
    else:
        raise AssertionError(f"no reference solution for {task_id}")


def verify(task, ws: Path, ledger: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(task.dir / "verify.py"), str(ws), str(ledger)], cwd=ws,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("task_id", sorted(TASKS))
def test_task_verifier(task_id, tmp_path):
    task = TASKS[task_id]
    ws, ledger = tmp_path / "ws", tmp_path / "ledger"
    task.prepare_workspace(ws)
    assert verify(task, ws, ledger).returncode != 0, "untouched workspace must fail"
    solve(task_id, ws, ledger, task.dir / "setup")
    result = verify(task, ws, ledger)
    assert result.returncode == 0, result.stdout + result.stderr
