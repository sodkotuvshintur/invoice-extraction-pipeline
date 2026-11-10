"""SQLite state store: schema creation, master-data load, small helpers."""
from __future__ import annotations

import json
import sqlite3
from importlib import resources
from pathlib import Path

from .vendor import normalise


def connect(path: str | Path = ":memory:") -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def create(conn: sqlite3.Connection) -> None:
    conn.executescript(resources.files("invoice_extractor").joinpath("schema.sql").read_text())


def load_master(conn: sqlite3.Connection, master: dict) -> None:
    """Load vendors, POs, receipts and bill history: the records AP checks invoices against."""
    for v in master["vendors"]:
        conn.execute("INSERT INTO vendors (id, name, category) VALUES (?, ?, ?)",
                     (v["id"], v["name"], v["category"]))
        conn.execute("INSERT INTO vendor_alias (vendor_id, alias) VALUES (?, ?)",
                     (v["id"], normalise(v["name"])))
    for po in master["purchase_orders"]:
        conn.execute("INSERT INTO purchase_orders (po_number, vendor_id, order_date) VALUES (?, ?, ?)",
                     (po["po_number"], po["vendor_id"], po.get("order_date", "")))
        for ln in po["lines"]:
            conn.execute(
                "INSERT INTO po_lines (po_number, sku, description, qty_ordered, unit_price, qty_received) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (po["po_number"], ln["sku"], ln["description"], str(ln["qty_ordered"]),
                 str(ln["unit_price"]), str(ln["qty_received"])))
    for b in master["bill_history"]:
        conn.execute("INSERT INTO bill_history (vendor_id, bill_date, amount) VALUES (?, ?, ?)",
                     (b["vendor_id"], str(b["bill_date"]), str(b["amount"])))
    conn.commit()


def fresh(path: str | Path, master_json: Path) -> sqlite3.Connection:
    """A new database with the schema and master data loaded."""
    if str(path) != ":memory:":
        Path(path).unlink(missing_ok=True)
    conn = connect(path)
    create(conn)
    load_master(conn, json.loads(Path(master_json).read_text()))
    return conn
