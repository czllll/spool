"""Shared plumbing for the demo services: the SQLite file, seeding, and the command line."""

from __future__ import annotations

import argparse
import contextlib
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Callable, Iterator

from ..mcp.server import Server


def connect(db: str | Path, schema: str) -> sqlite3.Connection:
    Path(db).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")  # a write the agent was told about survives a crash
    conn.executescript(schema + "\nCREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolled back on any exception (including a ToolError)."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def seed_once(conn: sqlite3.Connection, seed: str | None, load: Callable[[sqlite3.Connection, Any], None]) -> None:
    """Apply the seed file the first time only; a restarted server keeps the state it had."""
    if not seed or not Path(seed).exists() or conn.execute("SELECT 1 FROM meta WHERE key='seeded'").fetchone():
        return  # (a task that does not seed a service starts it empty)
    data = json.loads(Path(seed).read_text(encoding="utf-8"))
    with transaction(conn):
        load(conn, data)
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded', ?)", (seed,))


def dump(conn: sqlite3.Connection) -> dict[str, list[dict[str, Any]]]:
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name != 'meta' ORDER BY name")]
    return {t: [dict(r) for r in conn.execute(f"SELECT * FROM {t} ORDER BY rowid")] for t in tables}


def next_id(conn: sqlite3.Connection, prefix: str, table: str) -> str:
    """The next unused `<prefix><n>` id in `table` (seeded rows may already use low numbers)."""
    row = conn.execute("SELECT value FROM meta WHERE key=?", (f"seq:{prefix}",)).fetchone()
    n = int(row[0]) + 1 if row else 1
    while conn.execute(f"SELECT 1 FROM {table} WHERE id=?", (f"{prefix}{n}",)).fetchone():
        n += 1
    conn.execute("INSERT INTO meta(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (f"seq:{prefix}", str(n)))
    return f"{prefix}{n}"


def main(name: str, schema: str, load: Callable[[sqlite3.Connection, Any], None],
         build: Callable[[sqlite3.Connection], Server]) -> None:
    parser = argparse.ArgumentParser(prog=f"python -m spool.services.{name}")
    parser.add_argument("--db", required=True)
    parser.add_argument("--seed", help="JSON file applied the first time the database is created")
    parser.add_argument("command", nargs="?", choices=["serve", "show"], default="serve")
    args = parser.parse_args()
    conn = connect(args.db, schema)
    seed_once(conn, args.seed, load)
    if args.command == "show":
        json.dump(dump(conn), sys.stdout, indent=2, ensure_ascii=False)
        sys.stdout.write("\n")
        return
    build(conn).run_stdio()
