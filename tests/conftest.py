"""Shared fixtures: a tiny hand-built AP world, and a stub extraction engine.

Business-rule tests feed the pipeline a known ExtractedInvoice through
StubEngine, so they exercise the rules without touching a PDF or a model.
"""
from __future__ import annotations

import itertools
from datetime import date
from decimal import Decimal

import pytest

from invoice_extractor import db, pipeline
from invoice_extractor.models import ExtractedInvoice, Extraction, InvoiceLine

MASTER = {
    "vendors": [
        {"id": 1, "name": "Cascade Fastener Co.", "category": "inventory"},
        {"id": 2, "name": "Timberline Lumber Supply", "category": "inventory"},
        {"id": 3, "name": "Metro Power & Light", "category": "expense"},
    ],
    "purchase_orders": [
        {"po_number": "PO-1", "vendor_id": 1, "lines": [
            {"sku": "FST-1", "description": "Deck screw", "qty_ordered": 10, "unit_price": "20.00", "qty_received": 10},
            {"sku": "FST-2", "description": "Lag bolt", "qty_ordered": 4, "unit_price": "5.00", "qty_received": 2},
        ]},
        {"po_number": "PO-2", "vendor_id": 2, "lines": [
            {"sku": "LBR-1", "description": "Stud", "qty_ordered": 100, "unit_price": "4.00", "qty_received": 100},
        ]},
    ],
    "bill_history": [{"vendor_id": 3, "bill_date": f"2026-{m:02d}-01", "amount": str(1000 + m)} for m in range(1, 9)],
}

HIGH = {"vendor_name": 0.99, "invoice_number": 0.99, "invoice_date": 0.99, "total": 0.99, "lines": 0.99,
        "is_invoice": 0.99}


def invoice(vendor="Cascade Fastener Co.", number="INV-1", po="PO-1", lines=(("FST-1", 10, "20.00"),),
            total=None, tax="0.00") -> ExtractedInvoice:
    ls = [InvoiceLine(line_no=i, sku=sku, description=sku, qty=Decimal(qty), unit_price=Decimal(price),
                      amount=Decimal(qty) * Decimal(price)) for i, (sku, qty, price) in enumerate(lines, 1)]
    subtotal = sum((ln.amount for ln in ls), Decimal("0"))
    return ExtractedInvoice(vendor_name=vendor, invoice_number=number, invoice_date=date(2026, 9, 1), po_number=po,
                            subtotal=subtotal, tax=Decimal(tax),
                            total=Decimal(total) if total else subtotal + Decimal(tax), lines=ls)


class StubEngine:
    def __init__(self):
        self.next: Extraction | None = None

    def extract(self, pdf_bytes: bytes) -> Extraction:
        return self.next


@pytest.fixture
def conn():
    c = db.connect()
    db.create(c)
    db.load_master(c, MASTER)
    return c


@pytest.fixture
def submit(conn, tmp_path):
    """submit(invoice) -> Outcome. Each call is a new document with distinct bytes."""
    engine, cfg, seq = StubEngine(), pipeline.load_config(), itertools.count(1)

    def _submit(inv, confidence=None, engine_name="rules", content=None):
        n = next(seq)
        engine.next = Extraction(engine=engine_name, invoice=inv, field_confidence=confidence or HIGH)
        path = tmp_path / f"doc_{n:03d}.pdf"
        path.write_bytes(content or f"doc {n}".encode())
        return pipeline.process(conn, path, engine, cfg)

    return _submit
