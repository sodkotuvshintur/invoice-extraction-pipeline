"""The common invoice shape every extraction engine normalises to.

Downstream code (vendor resolution, validation, the decision gate) only ever
sees these models, so adding an engine never touches a business rule.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Decision(str, Enum):
    AUTO_PASS = "auto_pass"            # queued for the ledger, no human touch
    FLAG = "flag"                      # a named team has to look at it
    HOLD = "hold"                      # blocked outright (suspected duplicate)
    NOT_APPLICABLE = "not_applicable"  # not an invoice; recorded, nothing to do


class InvoiceLine(BaseModel):
    line_no: int
    sku: Optional[str] = None
    description: str = ""
    qty: Decimal
    unit_price: Decimal
    amount: Decimal


class ExtractedInvoice(BaseModel):
    is_invoice: bool = True
    vendor_name: Optional[str] = None
    invoice_number: Optional[str] = None
    invoice_date: Optional[date] = None
    due_date: Optional[date] = None
    po_number: Optional[str] = None
    subtotal: Optional[Decimal] = None
    tax: Optional[Decimal] = None
    total: Optional[Decimal] = None
    currency: str = "USD"
    lines: list[InvoiceLine] = Field(default_factory=list)


# The fields a bill cannot be posted without. One weak link drags the whole
# document to a human, so overall confidence is the minimum across these.
KEY_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "total", "lines")


class Extraction(BaseModel):
    """An engine's read of one document, plus how far each field can be trusted."""

    engine: str                       # "rules" | "llm" | "none"
    invoice: Optional[ExtractedInvoice] = None
    field_confidence: dict[str, float] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.invoice is not None

    @property
    def overall_confidence(self) -> float:
        if not self.invoice:
            return 0.0
        if not self.invoice.is_invoice:
            return self.field_confidence.get("is_invoice", 0.0)
        return min(self.field_confidence.get(f, 0.0) for f in KEY_FIELDS)


class RuleResult(BaseModel):
    """One check on one invoice. Every row is persisted, pass or fail."""

    rule: str
    passed: bool
    exception: Optional[str] = None   # exception type this failure raises
    expected: Optional[str] = None
    actual: Optional[str] = None
    detail: Optional[str] = None


class Outcome(BaseModel):
    decision: Decision
    exception: Optional[str] = None   # primary reason when not AUTO_PASS
    queue: Optional[str] = None       # the team that owns the exception
    reason: str = ""
    results: list[RuleResult] = Field(default_factory=list)
