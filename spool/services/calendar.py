"""Demo calendar: list, create and delete events (local times, YYYY-MM-DDTHH:MM)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Any

from ..mcp.server import Server, ToolError
from ._service import main, next_id, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, start TEXT NOT NULL, end TEXT NOT NULL,
    location TEXT, notes TEXT, idempotency_key TEXT UNIQUE
);
"""
INSTRUCTIONS = "The user's calendar. Times are local and written YYYY-MM-DDTHH:MM, e.g. 2026-10-20T15:30."


def load(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    for i, e in enumerate(data.get("events") or [], start=1):
        conn.execute("INSERT INTO events(id, title, start, end, location, notes) VALUES (?, ?, ?, ?, ?, ?)",
                     (e.get("id", f"e{i}"), e["title"], e["start"], e["end"], e.get("location"), e.get("notes")))


def _time(value: str, field: str) -> datetime:
    try:
        return datetime.strptime(value.strip(), "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ToolError(f"{field} must look like 2026-10-20T15:30, got {value!r}") from None


def build(conn: sqlite3.Connection) -> Server:
    server = Server("calendar", instructions=INSTRUCTIONS)

    @server.tool("list_events", "List events that overlap a date range (inclusive).", {
        "type": "object", "properties": {
            "from_date": {"type": "string", "description": "YYYY-MM-DD"},
            "to_date": {"type": "string", "description": "YYYY-MM-DD"},
        }, "required": ["from_date", "to_date"]}, annotations={"readOnlyHint": True, "openWorldHint": False})
    def list_events(args: dict[str, Any], meta: dict[str, Any]) -> str:
        lo, hi = args["from_date"].strip()[:10], args["to_date"].strip()[:10]
        rows = conn.execute("SELECT * FROM events WHERE substr(end, 1, 10) >= ? AND substr(start, 1, 10) <= ?"
                            " ORDER BY start", (lo, hi)).fetchall()
        if not rows:
            return f"No events between {lo} and {hi}."
        return "\n".join(f"{r['id']}  {r['start']} - {r['end'][11:]}  {r['title']}"
                         + (f"  @ {r['location']}" if r["location"] else "") for r in rows)

    @server.tool("create_event", "Add an event to the calendar.", {
        "type": "object", "properties": {
            "title": {"type": "string"},
            "start": {"type": "string", "description": "YYYY-MM-DDTHH:MM, local time"},
            "end": {"type": "string", "description": "YYYY-MM-DDTHH:MM, local time"},
            "location": {"type": "string"},
            "notes": {"type": "string"},
            "idempotency_key": {"type": "string"},
        }, "required": ["title", "start", "end"]},
        annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    def create_event(args: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        key = args.get("idempotency_key")
        if key:
            prior = conn.execute("SELECT id FROM events WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                return {"text": f"Already created as {prior['id']}.", "data": {"id": prior["id"], "deduplicated": True}}
        start, end = _time(args["start"], "start"), _time(args["end"], "end")
        if end <= start:
            raise ToolError("end must be after start")
        with transaction(conn):
            eid = next_id(conn, "e", "events")
            conn.execute("INSERT INTO events(id, title, start, end, location, notes, idempotency_key)"
                         " VALUES (?, ?, ?, ?, ?, ?, ?)",
                         (eid, args["title"].strip(), start.strftime("%Y-%m-%dT%H:%M"), end.strftime("%Y-%m-%dT%H:%M"),
                          args.get("location"), args.get("notes"), key))
        return {"text": f"Created {eid}: {args['title'].strip()}, {start:%a %d %b %H:%M}-{end:%H:%M}.",
                "data": {"id": eid, "deduplicated": False}}

    @server.tool("delete_event", "Delete an event.", {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        annotations={"readOnlyHint": False, "destructiveHint": True, "idempotentHint": True, "openWorldHint": False})
    def delete_event(args: dict[str, Any], meta: dict[str, Any]) -> str:
        n = conn.execute("DELETE FROM events WHERE id=?", (args["id"],)).rowcount
        return f"Deleted {args['id']}." if n else f"No event {args['id']!r} (nothing deleted)."

    return server


if __name__ == "__main__":
    main("calendar", SCHEMA, load, build)
