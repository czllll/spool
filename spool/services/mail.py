"""Demo mailbox: list and read messages, send mail, mark messages read."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..mcp.server import Server, ToolError
from ._service import main, next_id, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY, folder TEXT NOT NULL, sender TEXT NOT NULL, recipient TEXT NOT NULL,
    subject TEXT NOT NULL, body TEXT NOT NULL, date TEXT NOT NULL, unread INTEGER NOT NULL DEFAULT 1,
    in_reply_to TEXT, idempotency_key TEXT UNIQUE
);
"""
OWNER = "you@example.com"
INSTRUCTIONS = ("The user's mailbox. list_messages shows a folder, read_message shows one message, "
                "send_message sends an email from the user's address, mark_read marks messages as read.")


def load(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    for i, m in enumerate(data.get("inbox") or [], start=1):
        conn.execute("INSERT INTO messages(id, folder, sender, recipient, subject, body, date, unread)"
                     " VALUES (?, 'inbox', ?, ?, ?, ?, ?, ?)",
                     (m.get("id", f"m{i}"), m["from"], m.get("to", OWNER), m["subject"], m["body"], m["date"],
                      1 if m.get("unread", True) else 0))
    conn.execute("INSERT INTO meta(key, value) VALUES ('owner', ?) ON CONFLICT(key) DO NOTHING",
                 (data.get("owner", OWNER),))


def build(conn: sqlite3.Connection) -> Server:
    server = Server("mail", instructions=INSTRUCTIONS)
    owner = (conn.execute("SELECT value FROM meta WHERE key='owner'").fetchone() or [OWNER])[0]

    @server.tool("list_messages", "List messages in a folder, newest first.", {
        "type": "object", "properties": {
            "folder": {"type": "string", "enum": ["inbox", "sent"], "description": "Default inbox."},
            "unread_only": {"type": "boolean"},
            "query": {"type": "string", "description": "Only messages whose sender or subject contains this."},
        }}, annotations={"readOnlyHint": True, "openWorldHint": True})
    def list_messages(args: dict[str, Any], meta: dict[str, Any]) -> str:
        sql = "SELECT * FROM messages WHERE folder=?"
        params: list[Any] = [args.get("folder", "inbox")]
        if args.get("unread_only"):
            sql += " AND unread=1"
        if args.get("query"):
            sql += " AND (sender LIKE ? OR subject LIKE ?)"
            params += [f"%{args['query']}%"] * 2
        rows = conn.execute(sql + " ORDER BY date DESC", params).fetchall()
        if not rows:
            return "No messages."
        who = "to" if args.get("folder") == "sent" else "from"
        return "\n".join(f"{r['id']}  {r['date']}  {who} {r['recipient'] if who == 'to' else r['sender']}  "
                         f"{r['subject']}{'  [unread]' if r['unread'] else ''}" for r in rows)

    @server.tool("read_message", "Show one message with its headers and body.", {
        "type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        annotations={"readOnlyHint": True, "openWorldHint": True})
    def read_message(args: dict[str, Any], meta: dict[str, Any]) -> str:
        r = conn.execute("SELECT * FROM messages WHERE id=?", (args["id"],)).fetchone()
        if r is None:
            raise ToolError(f"no message with id {args['id']!r}")
        return (f"From: {r['sender']}\nTo: {r['recipient']}\nDate: {r['date']}\nSubject: {r['subject']}"
                + (f"\nIn-Reply-To: {r['in_reply_to']}" if r["in_reply_to"] else "") + f"\n\n{r['body']}")

    @server.tool("send_message", "Send an email from the user's address.", {
        "type": "object", "properties": {
            "to": {"type": "string", "description": "Recipient address."},
            "subject": {"type": "string"},
            "body": {"type": "string"},
            "in_reply_to": {"type": "string", "description": "Id of the message this replies to, if any."},
            "idempotency_key": {"type": "string", "description": "Same key, same message: a retry is not sent twice."},
        }, "required": ["to", "subject", "body"]},
        annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    def send_message(args: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        key = args.get("idempotency_key")
        if key:
            prior = conn.execute("SELECT id FROM messages WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                return {"text": f"Already sent as {prior['id']} (this is a repeat of the same request).",
                        "data": {"id": prior["id"], "deduplicated": True}}
        to = args["to"].strip()
        if "@" not in to:
            raise ToolError(f"{to!r} is not an email address")
        if args.get("in_reply_to") and not conn.execute("SELECT 1 FROM messages WHERE id=?", (args["in_reply_to"],)).fetchone():
            raise ToolError(f"in_reply_to: no message with id {args['in_reply_to']!r}")
        with transaction(conn):
            mid = next_id(conn, "s", "messages")
            conn.execute("INSERT INTO messages(id, folder, sender, recipient, subject, body, date, unread, in_reply_to,"
                         " idempotency_key) VALUES (?, 'sent', ?, ?, ?, ?, datetime('now'), 0, ?, ?)",
                         (mid, owner, to, args["subject"], args["body"], args.get("in_reply_to"), key))
        return {"text": f"Sent to {to} as {mid}.", "data": {"id": mid, "deduplicated": False}}

    @server.tool("mark_read", "Mark messages as read.", {
        "type": "object", "properties": {"ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]},
        annotations={"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False})
    def mark_read(args: dict[str, Any], meta: dict[str, Any]) -> str:
        n = 0
        for mid in args["ids"]:
            n += conn.execute("UPDATE messages SET unread=0 WHERE id=? AND folder='inbox'", (mid,)).rowcount
        return f"Marked {n} message(s) as read."

    return server


if __name__ == "__main__":
    main("mail", SCHEMA, load, build)
