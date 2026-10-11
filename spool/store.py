"""SQLite-backed, append-only event store.

Design notes
- One row per event, primary key (session_id, seq). `seq` is dense and starts at
  0; an append names the seq it expects to write, so two writers racing on the
  same session cannot both succeed (optimistic concurrency, no locks held).
- WAL journal + synchronous=FULL: once `append` returns, the event survives a
  kill -9 or an OS crash (on macOS, surviving a power cut also needs
  `fullfsync`). That durability is what lets the runtime treat "event appended"
  as "decision made" and replay everything else.
- The `sessions` table is a projection kept for listing (status, task) and for
  the scheduler (`due_at`: when a sleeping session wakes up or a pending
  approval lapses). It is rewritten in the same transaction as each event and
  can always be rebuilt from the events table.
- Routines are streams too (`routine:<name>`), with their own projection.
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
    n_events    INTEGER NOT NULL DEFAULT 0,
    due_at      REAL
);

CREATE TABLE IF NOT EXISTS routines (
    name          TEXT PRIMARY KEY,
    created_at    REAL NOT NULL,
    updated_at    REAL NOT NULL,
    enabled       INTEGER NOT NULL,
    deleted       INTEGER NOT NULL DEFAULT 0,
    spec          TEXT NOT NULL,
    fires         INTEGER NOT NULL DEFAULT 0,
    last_fired_at REAL,
    last_session  TEXT,
    n_events      INTEGER NOT NULL DEFAULT 0
);
"""

SESSION_COLUMNS = ["id", "created_at", "updated_at", "status", "task", "parent_id", "parent_seq", "workspace",
                   "n_events", "due_at"]
ROUTINE_COLUMNS = ["name", "created_at", "updated_at", "enabled", "deleted", "spec", "fires", "last_fired_at",
                   "last_session", "n_events"]


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
        self._migrate()
        self._conn.execute("CREATE INDEX IF NOT EXISTS sessions_due ON sessions(due_at) WHERE due_at IS NOT NULL")

    def _migrate(self) -> None:
        """Bring stores written by older versions up to date (projections only; events never change)."""
        columns = {row[1] for row in self._conn.execute("PRAGMA table_info(sessions)")}
        if "due_at" not in columns:
            self._conn.execute("ALTER TABLE sessions ADD COLUMN due_at REAL")
            waiting = self._conn.execute(
                "SELECT id FROM sessions WHERE status IN ('sleeping', 'awaiting_approval')").fetchall()
            for (sid,) in waiting:
                due = None
                for type, data in self._conn.execute(
                        "SELECT type, data FROM events WHERE session_id=? ORDER BY seq", (sid,)):
                    changed, value = _due_after(type, json.loads(data))
                    if changed:
                        due = value
                self._conn.execute("UPDATE sessions SET due_at=? WHERE id=?", (due, sid))

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
        if session_id.startswith(ev.ROUTINE_STREAM_PREFIX):
            self._project_routine(session_id[len(ev.ROUTINE_STREAM_PREFIX):], seq, type, data, ts)
            return
        if type in ev.ROUTINE_TYPES:
            raise ValueError(f"{type} belongs in a routine stream, not in session {session_id}")
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
        sets, values = ["updated_at=?", "n_events=?"], [ts, seq + 1]
        status = _status_after(type, data)
        if status is not None:
            sets.append("status=?")
            values.append(status)
        changed, due = _due_after(type, data)
        if changed:
            sets.append("due_at=?")
            values.append(due)
        self._conn.execute(f"UPDATE sessions SET {', '.join(sets)} WHERE id=?", (*values, session_id))

    def _project_routine(self, name: str, seq: int, type: str, data: dict[str, Any], ts: float) -> None:
        if type not in ev.ROUTINE_TYPES:
            raise ValueError(f"{type} is not a routine event")
        if type == ev.ROUTINE_SAVED:
            self._conn.execute(
                "INSERT INTO routines(name, created_at, updated_at, enabled, deleted, spec, n_events)"
                " VALUES (?,?,?,1,0,?,?)"
                " ON CONFLICT(name) DO UPDATE SET updated_at=excluded.updated_at, enabled=1, deleted=0,"
                " spec=excluded.spec, n_events=excluded.n_events",
                (name, ts, ts, canonical_json(data["spec"]), seq + 1),
            )
            return
        sets, values = ["updated_at=?", "n_events=?"], [ts, seq + 1]
        if type == ev.ROUTINE_FIRED:
            sets += ["fires=fires+1", "last_fired_at=?", "last_session=?"]
            values += [ts, data.get("session_id")]
        elif type == ev.ROUTINE_PAUSED:
            sets.append("enabled=0")
        elif type == ev.ROUTINE_RESUMED:
            sets.append("enabled=1")
        elif type == ev.ROUTINE_DELETED:
            sets += ["enabled=0", "deleted=1"]
        self._conn.execute(f"UPDATE routines SET {', '.join(sets)} WHERE name=?", (*values, name))

    # ------------------------------------------------------------------- reads
    def load(self, session_id: str, *, upto_seq: int | None = None, from_seq: int = 0) -> list[Event]:
        query = "SELECT session_id, seq, type, data, ts FROM events WHERE session_id=?"
        args: list[Any] = [session_id]
        if from_seq:
            query += " AND seq>=?"
            args.append(from_seq)
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
        query = f"SELECT {', '.join(SESSION_COLUMNS)} FROM sessions"
        if where:
            query += f" WHERE {where}"
        query += " ORDER BY updated_at DESC LIMIT ?"
        with self._lock:
            rows = self._conn.execute(query, (*args, limit)).fetchall()
        return [dict(zip(SESSION_COLUMNS, r)) for r in rows]

    def due_sessions(self, now: float, limit: int = 1000) -> list[str]:
        """Sessions whose wake-up time or approval deadline has passed, oldest first."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT id FROM sessions WHERE due_at IS NOT NULL AND due_at <= ? ORDER BY due_at LIMIT ?",
                (now, limit)).fetchall()
        return [r[0] for r in rows]

    def routines(self, *, include_deleted: bool = False) -> list[dict[str, Any]]:
        query = f"SELECT {', '.join(ROUTINE_COLUMNS)} FROM routines"
        if not include_deleted:
            query += " WHERE deleted=0"
        query += " ORDER BY name"
        with self._lock:
            rows = self._conn.execute(query).fetchall()
        out = []
        for r in rows:
            row = dict(zip(ROUTINE_COLUMNS, r))
            row["spec"] = json.loads(row["spec"])
            row["enabled"], row["deleted"] = bool(row["enabled"]), bool(row["deleted"])
            out.append(row)
        return out

    def routine(self, name: str) -> dict[str, Any] | None:
        return next((r for r in self.routines(include_deleted=True) if r["name"] == name), None)

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


def _due_after(type: str, data: dict[str, Any]) -> tuple[bool, float | None]:
    """(changed, value) of a session's due_at after this event."""
    if type == ev.TOOL_COMPLETED and data.get("wake_at") is not None:
        return True, float(data["wake_at"])
    if type == ev.APPROVAL_REQUESTED:
        return True, data.get("expires_at")
    if type in (ev.APPROVAL_DECIDED, ev.USER_MESSAGE, ev.SESSION_FINISHED) or (
            type == ev.NOTE and data.get("kind") == "woke"):
        return True, None
    return False, None
