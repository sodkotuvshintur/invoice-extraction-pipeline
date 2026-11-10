"""Engine B: Claude reads the PDF and returns a schema-constrained invoice.

Used only when no template recognises a document (new vendor, freeform
letter, scan). The model's output is constrained to `LlmInvoice` by
structured outputs, so there is no free text to parse and no JSON to repair.

The model is not asked how confident it is. Its answer is scored afterwards
by `rules.score`, which checks every value against the document's own text.
"""
from __future__ import annotations

import base64
import os
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Optional

from pydantic import BaseModel, Field

from ..models import ExtractedInvoice, Extraction, InvoiceLine
from . import rules

MODEL = os.environ.get("INVOICE_EXTRACTOR_MODEL", "claude-opus-5-5")

SYSTEM = """You extract accounts-payable documents for Harbor & Pine Supply Co., the buyer.

Read the attached PDF and fill the schema with what the document says.

- The vendor is the company asking to be paid, never Harbor & Pine.
- Copy identifiers (invoice number, PO number, item codes) character for character.
- Copy amounts exactly as printed, as plain decimals without currency symbols or
  thousands separators. Do not correct the vendor's arithmetic: if the printed total
  disagrees with the lines, report the printed total. A later step checks the maths,
  and it can only catch a vendor's error if you pass that error through unchanged.
- Dates are ISO 8601 (YYYY-MM-DD).
- Leave a field null when the document does not state it. A missing PO number is
  meaningful to the reviewers downstream; a guessed one would hide the problem.
- is_invoice is false for anything that is not itself a request for payment of
  specific goods or services: statements of account, remittance advice, quotes,
  order confirmations, delivery notes. For those, fill vendor_name and leave the rest."""


class LlmLine(BaseModel):
    sku: Optional[str] = Field(description="Item or service code, exactly as printed")
    description: str
    qty: str = Field(description="Quantity as a plain decimal")
    unit_price: str = Field(description="Price per unit as a plain decimal")
    amount: str = Field(description="Extended line amount as a plain decimal")


class LlmInvoice(BaseModel):
    is_invoice: bool
    vendor_name: Optional[str]
    invoice_number: Optional[str]
    invoice_date: Optional[str] = Field(description="YYYY-MM-DD")
    due_date: Optional[str] = Field(description="YYYY-MM-DD")
    po_number: Optional[str] = Field(description="The buyer's purchase order number, if stated")
    subtotal: Optional[str]
    tax: Optional[str]
    total: Optional[str] = Field(description="The amount the vendor is asking to be paid")
    lines: list[LlmLine]


def _dec(s: Optional[str]) -> Optional[Decimal]:
    if s is None:
        return None
    try:
        return Decimal(s.replace(",", "").replace("$", "").strip())
    except InvalidOperation:
        return None


def _date(s: Optional[str]) -> Optional[date]:
    try:
        return date.fromisoformat(s) if s else None
    except ValueError:
        return None


def to_invoice(raw: LlmInvoice) -> ExtractedInvoice:
    lines = []
    for ln in raw.lines:
        qty, price, amount = _dec(ln.qty), _dec(ln.unit_price), _dec(ln.amount)
        if qty is None or price is None or amount is None:
            continue
        lines.append(InvoiceLine(line_no=len(lines) + 1, sku=ln.sku, description=ln.description,
                                 qty=qty, unit_price=price, amount=amount))
    subtotal, tax, total = _dec(raw.subtotal), _dec(raw.tax), _dec(raw.total)
    if raw.is_invoice and lines:
        # Small suppliers often print one figure. Filling in the subtotal from the
        # lines is arithmetic, not guessing; the printed total is never touched.
        subtotal = subtotal if subtotal is not None else sum((l.amount for l in lines), Decimal("0"))
        if tax is None and total is not None:
            tax = total - subtotal
    return ExtractedInvoice(
        is_invoice=raw.is_invoice, vendor_name=raw.vendor_name, invoice_number=raw.invoice_number,
        invoice_date=_date(raw.invoice_date), due_date=_date(raw.due_date), po_number=raw.po_number,
        subtotal=subtotal, tax=tax, total=total, lines=lines,
    )


class LlmExtractor:
    """`client` is an anthropic.Anthropic; injected so tests run without a key."""

    def __init__(self, client: Any = None, model: str = MODEL):
        if client is None:
            import anthropic
            client = anthropic.Anthropic()
        self.client = client
        self.model = model

    def extract(self, pdf_bytes: bytes) -> Extraction:
        import anthropic

        try:
            response = self.client.beta.messages.parse(
                model=self.model,
                max_tokens=16000,
                system=SYSTEM,
                output_config={"effort": "low"},  # transcription, not reasoning
                output_format=LlmInvoice,
                # if a safety classifier declines, retry on another model server-side
                betas=["server-side-fallback-2026-07-01"],
                fallbacks="default",
                messages=[{"role": "user", "content": [
                    {"type": "document", "source": {
                        "type": "base64", "media_type": "application/pdf",
                        "data": base64.standard_b64encode(pdf_bytes).decode()}},
                    {"type": "text", "text": "Extract this document."},
                ]}],
            )
        except anthropic.APIConnectionError as e:
            return Extraction(engine="llm", notes=[f"model unreachable: {e}"])
        except anthropic.APIStatusError as e:
            return Extraction(engine="llm", notes=[f"model error {e.status_code}: {e.message}"])

        raw = response.parsed_output
        if response.stop_reason != "end_turn" or raw is None:
            return Extraction(engine="llm", notes=[f"no usable output (stop_reason={response.stop_reason})"])

        inv = to_invoice(raw)
        text = rules.pdf_text(pdf_bytes)
        conf = rules.score(inv, text)
        notes = [f"model={response.model}"]
        if not inv.is_invoice:
            # A wrong "not an invoice" silently drops a real bill, which is the one
            # mistake nobody reviews. Only the template engine may dismiss a document
            # outright; an LLM dismissal is capped so a person confirms it.
            conf["is_invoice"] = 0.80
        return Extraction(engine="llm", invoice=inv, field_confidence=conf, notes=notes)
