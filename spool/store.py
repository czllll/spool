"""SQLite-backed, append-only event store.

Design notes
- One row per event, primary key (session_id, seq). `seq` is dense and starts at
  0; an append names the seq it expects to write, so two writers racing on the
  same session cannot both succeed (optimistic concurrency, no locks held).
- WAL journal + synchronous=FULL: once `append` returns, the event survives a
  kill -9 or an OS crash (on macOS, surviving a power cut also needs
  `fullfsync`). That durability is what lets the runtime treat "event appended"
  as "decision made" and replay everything else.
- The `sessions` table is a projection kept for listing (status, task). It is
  rewritten in the same transaction as each event and can always be rebuilt
  from the events table.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

from . import events as ev
from .events import Event
from .util import canonical_json, now

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    session_id TEXT    NOT NULL,
    seq        INTEGER NOT NULL,
    type       TEXT    NOT NULL,
    data       TEXT    NOT NULL,
    ts         REAL    NOT NULL,
    PRIMARY KEY (session_id, seq)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS sessions (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    status      TEXT NOT NULL,
    task        TEXT NOT NULL,
    parent_id   TEXT,
    parent_seq  INTEGER,
    workspace   TEXT,
    n_events    INTEGER NOT NULL DEFAULT 0
);
"""


class ConcurrencyError(RuntimeError):
    """Another writer appended to the session first (seq already taken)."""


class EventStore:
    def __init__(self, path: str | Path, *, synchronous: str = "FULL") -> None:
        self.path = Path(path)
        if str(path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False, timeout=30)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute(f"PRAGMA synchronous={synchronous}")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.executescript(SCHEMA)

    # ------------------------------------------------------------------ writes
    def append(
        self,
        session_id: str,
        type: str,
        data: dict[str, Any],
        *,
        expected_seq: int,
        ts: float | None = None,
    ) -> Event:
        if type not in ev.ALL_TYPES:
            raise ValueError(f"unknown event type: {type}")
        ts = now() if ts is None else ts
        payload = canonical_json(data)
        with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "INSERT INTO events(session_id, seq, type, data, ts) VALUES (?,?,?,?,?)",
                    (session_id, expected_seq, type, payload, ts),
                )
                self._project(session_id, expected_seq, type, data, ts)
                self._conn.execute("COMMIT")
            except sqlite3.IntegrityError as exc:
                self._conn.execute("ROLLBACK")
                raise ConcurrencyError(
                    f"session {session_id}: seq {expected_seq} already written by another writer"
                ) from exc
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
        return Event(session_id=session_id, seq=expected_seq, type=type, data=json.loads(payload), ts=ts)

    def _project(self, session_id: str, seq: int, type: str, data: dict[str, Any], ts: float) -> None:
        if type == ev.SESSION_STARTED:
            parent = data.get("parent") or {}
            self._conn.execute(
                "INSERT INTO sessions(id, created_at, updated_at, status, task, parent_id, parent_seq, workspace, n_events)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    session_id,
                    ts,
                    ts,
                    "running",
                    data.get("task", ""),
                    parent.get("session_id"),
                    parent.get("seq"),
                    data.get("workspace"),
                    seq + 1,
                ),
            )
            return
        status = _status_after(type, data)
        if status is None:
            self._conn.execute(
                "UPDATE sessions SET updated_at=?, n_events=? WHERE id=?", (ts, seq + 1, session_id)
            )
        else:
            self._conn.execute(
                "UPDATE sessions SET updated_at=?, n_events=?, status=? WHERE id=?",
                (ts, seq + 1, status, session_id),
            )

    # ------------------------------------------------------------------- reads
    def load(self, session_id: str, *, upto_seq: int | None = None) -> list[Event]:
        query = "SELECT session_id, seq, type, data, ts FROM events WHERE session_id=?"
        args: list[Any] = [session_id]
        if upto_seq is not None:
            query += " AND seq<=?"
            args.append(upto_seq)
        query += " ORDER BY seq"
        with self._lock:
            rows = self._conn.execute(query, args).fetchall()
        return [Event(r[0], r[1], r[2], json.loads(r[3]), r[4]) for r in rows]

    def exists(self, session_id: str) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT 1 FROM sessions WHERE id=?", (session_id,)).fetchone()
        return row is not None

    def session(self, session_id: str) -> dict[str, Any] | None:
        rows = self.sessions(where="id=?", args=(session_id,), limit=1)
        return rows[0] if rows else None

    def sessions(self, *, where: str = "", args: Iterable[Any] = (), limit: int = 50) -> list[dict[str, Any]]:
        query = "SELECT id, created_at, updated_at, status, task, parent_id, parent_seq, workspace, n_events FROM sessions"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*args, limit)).fetchall()
        keys = ["id", "created_at", "updated_at", "status", "task", "parent_id", "parent_seq", "workspace", "n_events"]
        return [dict(zip(keys, r)) for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _status_after(type: str, data: dict[str, Any]) -> str | None:
    if type == ev.SESSION_FINISHED:
        return data.get("status", "completed")
    if type == ev.APPROVAL_REQUESTED:
        return "awaiting_approval"
    if type in (ev.APPROVAL_DECIDED, ev.USER_MESSAGE):
        return "running"
    if type == ev.TOOL_COMPLETED and data.get("wake_at") is not None:
        return "sleeping"
    if type == ev.NOTE and data.get("kind") == "woke":
        return "running"
    return None
