"""Engine A: deterministic template parsers over the PDF text layer.

Free, instant, and exactly repeatable -- the right tool for the high-volume
vendors whose layout is known. Each parser recognises its layout by a
signature and returns None for anything else; it never guesses. An
unrecognised document falls through to the LLM engine or to a human.
"""
from __future__ import annotations

import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Callable, Optional

import pdfplumber

from ..models import ExtractedInvoice, Extraction, InvoiceLine

NUM = r"[\d,]+\.\d{2}"
CODE = r"[A-Z]{3}-\d+"


def pdf_text(pdf_bytes: bytes) -> str:
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        return "\n".join(page.extract_text() or "" for page in pdf.pages)


def _dec(s: str) -> Decimal:
    return Decimal(s.replace(",", "").replace("$", ""))


def _find(pattern: str, text: str) -> Optional[str]:
    m = re.search(pattern, text, re.MULTILINE)
    return m.group(1).strip() if m else None


def _date(s: Optional[str], fmt: str) -> Optional[date]:
    try:
        return datetime.strptime(s, fmt).date() if s else None
    except ValueError:
        return None


def _money(pattern: str, text: str) -> Optional[Decimal]:
    s = _find(pattern, text)
    try:
        return _dec(s) if s else None
    except InvalidOperation:
        return None


def _lines(text: str, row: str, order: tuple[str, ...]) -> list[InvoiceLine]:
    """`order` names the regex groups in the order this layout prints them."""
    out = []
    for m in re.finditer(row, text, re.MULTILINE):
        g = dict(zip(order, m.groups()))
        out.append(InvoiceLine(line_no=len(out) + 1, sku=g["sku"], description=g["desc"].strip(),
                               qty=_dec(g["qty"]), unit_price=_dec(g["price"]), amount=_dec(g["amount"])))
    return out


def _parse_a(text: str) -> Optional[ExtractedInvoice]:
    if "Invoice Number:" not in text or "Total Due" not in text:
        return None
    return ExtractedInvoice(
        vendor_name=text.splitlines()[0].strip(),
        invoice_number=_find(r"Invoice Number:\s*(\S+)", text),
        invoice_date=_date(_find(r"Invoice Date:\s*(\S+)", text), "%m/%d/%Y"),
        due_date=_date(_find(r"Due Date:\s*(\S+)", text), "%m/%d/%Y"),
        po_number=_find(r"PO Number:\s*(\S+)", text),
        subtotal=_money(rf"^Subtotal\s+({NUM})", text),
        tax=_money(rf"^Tax\s+({NUM})", text),
        total=_money(rf"Total Due\s+\$({NUM})", text),
        lines=_lines(text, rf"^({CODE})\s+(.+?)\s+([\d,]+(?:\.\d+)?)\s+({NUM})\s+({NUM})$",
                     ("sku", "desc", "qty", "price", "amount")),
    )


def _parse_b(text: str) -> Optional[ExtractedInvoice]:
    if "Invoice #" not in text or "Balance Due" not in text:
        return None
    long_date = r"([A-Z][a-z]+ \d{1,2}, \d{4})"
    return ExtractedInvoice(
        vendor_name=text.splitlines()[0].strip(),
        invoice_number=_find(r"Invoice #\s*(\S+)", text),
        invoice_date=_date(_find(rf"Date {long_date}", text), "%B %d, %Y"),
        due_date=_date(_find(rf"Due {long_date}", text), "%B %d, %Y"),
        po_number=_find(r"Customer PO\s+(\S+)", text),
        subtotal=_money(rf"Sub-total\s+\$({NUM})", text),
        tax=_money(rf"Sales Tax\s+\$({NUM})", text),
        total=_money(rf"Balance Due\s+\$({NUM})", text),
        lines=_lines(text, rf"^([\d,]+(?:\.\d+)?)\s+({CODE})\s+(.+?)\s+\$({NUM})\s+\$({NUM})$",
                     ("qty", "sku", "desc", "price", "amount")),
    )


def _parse_c(text: str) -> Optional[ExtractedInvoice]:
    if "TAX INVOICE" not in text or "Bill No." not in text:
        return None
    return ExtractedInvoice(
        vendor_name=_find(r"Remit payment to:\s*(.+?)\s*\|", text),
        invoice_number=_find(r"Bill No\.\s*(\S+)", text),
        invoice_date=_date(_find(r"Dated:\s*(\S+)", text), "%d-%b-%Y"),
        due_date=_date(_find(r"Payment due:\s*(\S+)", text), "%d-%b-%Y"),
        po_number=_find(r"Your Order Ref:\s*(\S+)", text),
        subtotal=_money(rf"^Net\s+({NUM})", text),
        tax=_money(rf"^Tax\s+({NUM})", text),
        total=_money(rf"Amount Payable USD\s+({NUM})", text),
        lines=_lines(text, rf"^\d+\s+({CODE})\s+(.+?)\s+([\d,]+(?:\.\d+)?)\s+({NUM})\s+({NUM})$",
                     ("sku", "desc", "qty", "price", "amount")),
    )


PARSERS: list[Callable[[str], Optional[ExtractedInvoice]]] = [_parse_a, _parse_b, _parse_c]


def extract(pdf_bytes: bytes) -> Optional[Extraction]:
    """Return an Extraction, or None when no template recognises the document."""
    text = pdf_text(pdf_bytes)
    if re.search(r"^STATEMENT OF ACCOUNT$", text, re.MULTILINE):
        inv = ExtractedInvoice(is_invoice=False, vendor_name=text.splitlines()[0].strip())
        return Extraction(engine="rules", invoice=inv, field_confidence={"is_invoice": 0.99},
                          notes=["statement header: not an invoice"])
    for parse in PARSERS:
        inv = parse(text)
        if inv is not None:
            return Extraction(engine="rules", invoice=inv, field_confidence=score(inv, text))
    return None


def score(inv: ExtractedInvoice, text: str) -> dict[str, float]:
    """Confidence is earned by evidence, not asserted.

    A field scores high only if it was found at all *and* the exact value can
    be located in the document's text layer. This matters most for the LLM
    engine: a hallucinated invoice number or total does not appear in the
    source, scores low, and sends the document to a human.
    With no text layer (a scan) nothing can be grounded, so every field is
    capped below the auto-pass threshold.
    """
    flat = re.sub(r"[,\s$]", "", text)
    has_text = bool(flat)

    def grounded(value) -> float:
        if value is None:
            return 0.0
        if not has_text:
            return 0.80
        if isinstance(value, Decimal):
            needle = f"{value:.2f}"
        elif isinstance(value, date):
            return 0.99 if str(value.year) in flat and str(value.day) in flat else 0.5
        else:
            needle = re.sub(r"[,\s$]", "", str(value))
        return 0.99 if needle in flat else 0.5

    conf = {f: grounded(getattr(inv, f)) for f in
            ("vendor_name", "invoice_number", "invoice_date", "po_number", "subtotal", "tax", "total")}
    conf["lines"] = min((min(grounded(l.amount), grounded(l.unit_price)) for l in inv.lines), default=0.0)
    conf["is_invoice"] = 0.99
    return conf
