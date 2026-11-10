"""Render synthetic documents as PDFs.

Four invoice layouts plus a statement, because real AP inboxes are not one
template: labels, column order, date formats and even where the vendor's name
appears all differ by supplier. Layout D is prose, which no template parser
should be expected to read.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

BUYER = "Harbor & Pine Supply Co."
BUYER_ADDR = "400 Wharf Street, Port Alder, WA 98000"
W, H = letter
LEFT, RIGHT = 54, W - 54


def money(v: Decimal) -> str:
    return f"{v:,.2f}"


def qty_str(v: Decimal) -> str:
    return f"{v:,.0f}" if v == v.to_integral_value() else f"{v:,.2f}"


def _canvas(path: Path) -> canvas.Canvas:
    # invariant=1: no timestamps in the file, so the same seed gives the same bytes
    return canvas.Canvas(str(path), pagesize=letter, invariant=1)


def _rule(c: canvas.Canvas, y: float) -> None:
    c.setLineWidth(0.5)
    c.line(LEFT, y, RIGHT, y)


def render_a(doc: dict, path: Path) -> None:
    """Classic: vendor at top, labelled header block, Item/Description/Qty/Unit Price/Amount."""
    c = _canvas(path)
    y = H - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(LEFT, y, doc["vendor_name"])
    c.setFont("Helvetica", 9)
    c.drawString(LEFT, y - 14, doc["vendor_addr"])
    y -= 48
    c.setFont("Helvetica-Bold", 13)
    c.drawString(LEFT, y, "INVOICE")
    c.setFont("Helvetica", 10)
    y -= 20
    header = [
        ("Invoice Number:", doc["invoice_number"]),
        ("Invoice Date:", doc["invoice_date"].strftime("%m/%d/%Y")),
        ("Due Date:", doc["due_date"].strftime("%m/%d/%Y")),
    ]
    if doc.get("po_number"):
        header.append(("PO Number:", doc["po_number"]))
    for label, value in header:
        c.drawString(LEFT, y, f"{label} {value}")
        y -= 14
    y -= 6
    c.drawString(LEFT, y, f"Bill To: {BUYER}")
    c.drawString(LEFT, y - 12, BUYER_ADDR)
    y -= 40
    c.setFont("Helvetica-Bold", 9)
    c.drawString(LEFT, y, "Item")
    c.drawString(LEFT + 70, y, "Description")
    c.drawRightString(RIGHT - 170, y, "Qty")
    c.drawRightString(RIGHT - 85, y, "Unit Price")
    c.drawRightString(RIGHT, y, "Amount")
    _rule(c, y - 4)
    c.setFont("Helvetica", 9)
    y -= 18
    for ln in doc["lines"]:
        c.drawString(LEFT, y, ln["sku"])
        c.drawString(LEFT + 70, y, ln["description"])
        c.drawRightString(RIGHT - 170, y, qty_str(ln["qty"]))
        c.drawRightString(RIGHT - 85, y, money(ln["unit_price"]))
        c.drawRightString(RIGHT, y, money(ln["amount"]))
        y -= 14
    _rule(c, y + 6)
    y -= 12
    for label, value in (("Subtotal", money(doc["subtotal"])), ("Tax", money(doc["tax"]))):
        c.drawRightString(RIGHT - 85, y, label)
        c.drawRightString(RIGHT, y, value)
        y -= 14
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(RIGHT - 85, y, "Total Due")
    c.drawRightString(RIGHT, y, "$" + money(doc["total"]))
    c.setFont("Helvetica", 8)
    c.drawString(LEFT, 60, f"Terms: Net {doc['terms_days']}. Thank you for your business.")
    c.save()


def render_b(doc: dict, path: Path) -> None:
    """Modern: long-form dates, quantity first, dollar signs, 'Balance Due'."""
    c = _canvas(path)
    y = H - 56
    c.setFont("Helvetica-Bold", 18)
    c.drawString(LEFT, y, doc["vendor_name"])
    c.setFont("Helvetica", 9)
    c.drawString(LEFT, y - 14, doc["vendor_addr"])
    y -= 50
    c.setFont("Helvetica", 10)
    header = [
        ("Invoice #", doc["invoice_number"]),
        ("Date", f"{doc['invoice_date']:%B} {doc['invoice_date'].day}, {doc['invoice_date'].year}"),
        ("Due", f"{doc['due_date']:%B} {doc['due_date'].day}, {doc['due_date'].year}"),
    ]
    if doc.get("po_number"):
        header.append(("Customer PO", doc["po_number"]))
    for label, value in header:
        c.drawRightString(RIGHT, y, f"{label} {value}")
        y -= 14
    c.drawString(LEFT, y + 28, "Sold to")
    c.drawString(LEFT, y + 16, BUYER)
    c.drawString(LEFT, y + 4, BUYER_ADDR)
    y -= 30
    c.setFont("Helvetica-Bold", 9)
    c.drawString(LEFT, y, "Qty")
    c.drawString(LEFT + 50, y, "Item")
    c.drawString(LEFT + 120, y, "Description")
    c.drawRightString(RIGHT - 85, y, "Rate")
    c.drawRightString(RIGHT, y, "Total")
    _rule(c, y - 4)
    c.setFont("Helvetica", 9)
    y -= 18
    for ln in doc["lines"]:
        c.drawString(LEFT, y, qty_str(ln["qty"]))
        c.drawString(LEFT + 50, y, ln["sku"])
        c.drawString(LEFT + 120, y, ln["description"])
        c.drawRightString(RIGHT - 85, y, "$" + money(ln["unit_price"]))
        c.drawRightString(RIGHT, y, "$" + money(ln["amount"]))
        y -= 14
    _rule(c, y + 6)
    y -= 12
    rows = (("Sub-total", doc["subtotal"]), ("Sales Tax", doc["tax"]), ("Balance Due", doc["total"]))
    for label, value in rows:
        c.drawRightString(RIGHT - 85, y, label)
        c.drawRightString(RIGHT, y, "$" + money(value))
        y -= 14
    c.setFont("Helvetica", 8)
    c.drawString(LEFT, 60, f"Payment terms net {doc['terms_days']} days.")
    c.save()


def render_c(doc: dict, path: Path) -> None:
    """Ledger style: 'TAX INVOICE', numbered rows, vendor only in the remittance footer."""
    c = _canvas(path)
    y = H - 60
    c.setFont("Helvetica-Bold", 14)
    c.drawCentredString(W / 2, y, "TAX INVOICE")
    c.setFont("Helvetica", 10)
    y -= 30
    header = [
        f"Bill No. {doc['invoice_number']}",
        f"Dated: {doc['invoice_date']:%d-%b-%Y}",
        f"Payment due: {doc['due_date']:%d-%b-%Y}",
    ]
    if doc.get("po_number"):
        header.append(f"Your Order Ref: {doc['po_number']}")
    for text in header:
        c.drawString(LEFT, y, text)
        y -= 14
    y -= 6
    c.drawString(LEFT, y, f"Customer: {BUYER}")
    y -= 30
    c.setFont("Helvetica-Bold", 9)
    c.drawString(LEFT, y, "#")
    c.drawString(LEFT + 24, y, "Code")
    c.drawString(LEFT + 94, y, "Description")
    c.drawRightString(RIGHT - 170, y, "Quantity")
    c.drawRightString(RIGHT - 85, y, "Price")
    c.drawRightString(RIGHT, y, "Extended")
    _rule(c, y - 4)
    c.setFont("Helvetica", 9)
    y -= 18
    for i, ln in enumerate(doc["lines"], 1):
        c.drawString(LEFT, y, str(i))
        c.drawString(LEFT + 24, y, ln["sku"])
        c.drawString(LEFT + 94, y, ln["description"])
        c.drawRightString(RIGHT - 170, y, qty_str(ln["qty"]))
        c.drawRightString(RIGHT - 85, y, money(ln["unit_price"]))
        c.drawRightString(RIGHT, y, money(ln["amount"]))
        y -= 14
    _rule(c, y + 6)
    y -= 12
    c.drawRightString(RIGHT, y, f"Net {money(doc['subtotal'])}")
    c.drawRightString(RIGHT, y - 14, f"Tax {money(doc['tax'])}")
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(RIGHT, y - 30, f"Amount Payable USD {money(doc['total'])}")
    c.setFont("Helvetica", 8)
    c.drawString(LEFT, 60, f"Remit payment to: {doc['vendor_name']} | {doc['vendor_addr']}")
    c.save()


def render_d(doc: dict, path: Path) -> None:
    """Freeform: a small supplier's invoice written as a letter. No table, no labels."""
    c = _canvas(path)
    y = H - 60
    c.setFont("Times-Bold", 15)
    c.drawString(LEFT, y, doc["vendor_name"])
    c.setFont("Times-Roman", 10)
    c.drawString(LEFT, y - 14, doc["vendor_addr"])
    y -= 54
    c.setFont("Times-Roman", 11)
    d = doc["invoice_date"]
    paragraphs = [
        f"To the accounts team at {BUYER},",
        "",
        f"This is our invoice no. {doc['invoice_number']}, written {d.day} {d:%B} {d.year}.",
    ]
    if doc.get("po_number"):
        paragraphs.append(f"It covers what we delivered against your order {doc['po_number']}:")
    else:
        paragraphs.append("It covers what we delivered this month:")
    paragraphs.append("")
    for ln in doc["lines"]:
        paragraphs.append(
            f"  {qty_str(ln['qty'])} of {ln['description']} ({ln['sku']}) at "
            f"${money(ln['unit_price'])} apiece, which comes to ${money(ln['amount'])}."
        )
    due = doc["due_date"]
    paragraphs += [
        "",
        f"All together that is ${money(doc['total'])}, with no tax charged.",
        f"We would appreciate payment by {due.day} {due:%B} {due.year}.",
        "",
        "With thanks,",
        doc["vendor_name"],
    ]
    for text in paragraphs:
        c.drawString(LEFT, y, text)
        y -= 16
    c.save()


def render_statement(doc: dict, path: Path) -> None:
    """A statement of account: lists invoices, requests nothing new. Must not be paid."""
    c = _canvas(path)
    y = H - 60
    c.setFont("Helvetica-Bold", 16)
    c.drawString(LEFT, y, doc["vendor_name"])
    c.setFont("Helvetica", 9)
    c.drawString(LEFT, y - 14, doc["vendor_addr"])
    y -= 48
    c.setFont("Helvetica-Bold", 13)
    c.drawString(LEFT, y, "STATEMENT OF ACCOUNT")
    c.setFont("Helvetica", 10)
    y -= 20
    c.drawString(LEFT, y, f"Statement Date: {doc['invoice_date']:%m/%d/%Y}")
    c.drawString(LEFT, y - 14, f"Account: {BUYER}")
    y -= 44
    c.setFont("Helvetica-Bold", 9)
    c.drawString(LEFT, y, "Invoice")
    c.drawString(LEFT + 120, y, "Date")
    c.drawRightString(RIGHT, y, "Open Amount")
    _rule(c, y - 4)
    c.setFont("Helvetica", 9)
    y -= 18
    for number, d, amount in doc["open_items"]:
        c.drawString(LEFT, y, number)
        c.drawString(LEFT + 120, y, f"{d:%m/%d/%Y}")
        c.drawRightString(RIGHT, y, money(amount))
        y -= 14
    _rule(c, y + 6)
    c.setFont("Helvetica-Bold", 10)
    c.drawRightString(RIGHT, y - 12, f"Account Balance {money(doc['total'])}")
    c.setFont("Helvetica", 8)
    c.drawString(LEFT, 60, "This is a statement, not an invoice. Invoices listed were sent separately.")
    c.save()


RENDERERS = {"A": render_a, "B": render_b, "C": render_c, "D": render_d, "statement": render_statement}


def render(doc: dict, path: Path) -> None:
    RENDERERS[doc["layout"]](doc, path)


__all__ = ["render", "BUYER", "date"]
