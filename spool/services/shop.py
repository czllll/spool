"""Demo shop: search products, place orders (shipped to the user's saved address)."""

from __future__ import annotations

import sqlite3
from typing import Any

from ..mcp.server import Server, ToolError
from ._service import main, next_id, transaction

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    sku TEXT PRIMARY KEY, name TEXT NOT NULL, price_cents INTEGER NOT NULL, in_stock INTEGER NOT NULL,
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY, sku TEXT NOT NULL, quantity INTEGER NOT NULL, total_cents INTEGER NOT NULL,
    created_at TEXT NOT NULL, idempotency_key TEXT UNIQUE
);
"""
INSTRUCTIONS = "An online shop. Prices are in US dollars; orders ship to the user's saved address."


def load(conn: sqlite3.Connection, data: dict[str, Any]) -> None:
    for p in data.get("products") or []:
        conn.execute("INSERT INTO products(sku, name, price_cents, in_stock, description) VALUES (?, ?, ?, ?, ?)",
                     (p["sku"], p["name"], int(round(p["price"] * 100)), int(p.get("in_stock", 10)),
                      p.get("description", "")))


def _money(cents: int) -> str:
    return f"${cents / 100:.2f}"


def build(conn: sqlite3.Connection) -> Server:
    server = Server("shop", instructions=INSTRUCTIONS)

    @server.tool("search_products", "Search the catalog; every word of the query must match the name or description.", {
        "type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        annotations={"readOnlyHint": True, "openWorldHint": True})
    def search_products(args: dict[str, Any], meta: dict[str, Any]) -> str:
        words = [w.lower() for w in args["query"].split() if w.strip()]
        rows = [r for r in conn.execute("SELECT * FROM products ORDER BY price_cents")
                if all(w in (r["name"] + " " + r["description"]).lower() for w in words)]
        if not rows:
            return "No products match."
        return "\n".join(f"{r['sku']}  {r['name']}  {_money(r['price_cents'])}  "
                         f"{'in stock' if r['in_stock'] > 0 else 'OUT OF STOCK'}" for r in rows)

    @server.tool("get_product", "Show one product with its description.", {
        "type": "object", "properties": {"sku": {"type": "string"}}, "required": ["sku"]},
        annotations={"readOnlyHint": True, "openWorldHint": True})
    def get_product(args: dict[str, Any], meta: dict[str, Any]) -> str:
        r = conn.execute("SELECT * FROM products WHERE sku=?", (args["sku"],)).fetchone()
        if r is None:
            raise ToolError(f"no product {args['sku']!r}")
        stock = f"{r['in_stock']} in stock" if r["in_stock"] > 0 else "out of stock"
        return f"{r['sku']}: {r['name']}\nPrice: {_money(r['price_cents'])}\nStock: {stock}\n\n{r['description']}"

    @server.tool("place_order", "Buy a product. Charges the user's saved card.", {
        "type": "object", "properties": {
            "sku": {"type": "string"},
            "quantity": {"type": "integer"},
            "idempotency_key": {"type": "string"},
        }, "required": ["sku", "quantity"]},
        annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": True})
    def place_order(args: dict[str, Any], meta: dict[str, Any]) -> dict[str, Any]:
        key = args.get("idempotency_key")
        if key:
            prior = conn.execute("SELECT * FROM orders WHERE idempotency_key=?", (key,)).fetchone()
            if prior:
                return {"text": f"Already ordered as {prior['id']} (this is a repeat of the same request).",
                        "data": {"id": prior["id"], "deduplicated": True}}
        qty = int(args["quantity"])
        if qty < 1:
            raise ToolError("quantity must be at least 1")
        with transaction(conn):
            r = conn.execute("SELECT * FROM products WHERE sku=?", (args["sku"],)).fetchone()
            if r is None or r["in_stock"] < qty:
                raise ToolError(f"{args['sku']} is not available" if r is None else f"{args['sku']} is out of stock")
            oid = next_id(conn, "o", "orders")
            total = r["price_cents"] * qty
            conn.execute("UPDATE products SET in_stock = in_stock - ? WHERE sku=?", (qty, r["sku"]))
            conn.execute("INSERT INTO orders(id, sku, quantity, total_cents, created_at, idempotency_key)"
                         " VALUES (?, ?, ?, ?, datetime('now'), ?)", (oid, r["sku"], qty, total, key))
        return {"text": f"Ordered {qty} x {r['name']} for {_money(total)} (order {oid}).",
                "data": {"id": oid, "total": total / 100, "deduplicated": False}}

    @server.tool("list_orders", "List the orders placed so far.", {"type": "object", "properties": {}},
                 annotations={"readOnlyHint": True, "openWorldHint": False})
    def list_orders(args: dict[str, Any], meta: dict[str, Any]) -> str:
        rows = conn.execute("SELECT o.*, p.name FROM orders o JOIN products p USING (sku) ORDER BY o.rowid").fetchall()
        if not rows:
            return "No orders yet."
        return "\n".join(f"{r['id']}  {r['quantity']} x {r['name']} ({r['sku']})  {_money(r['total_cents'])}" for r in rows)

    return server


if __name__ == "__main__":
    main("shop", SCHEMA, load, build)
