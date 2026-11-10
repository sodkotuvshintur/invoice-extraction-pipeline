"""The rulebooks. Deterministic on purpose: this is the code that decides
whether money moves, so every comparison is plain arithmetic against a
threshold in tolerances.json, and every check leaves a row behind.

No model output is trusted here beyond the extracted fields themselves, and
those have already had to clear the confidence gate.
"""
from __future__ import annotations

import sqlite3
from decimal import Decimal
from statistics import mean, pstdev

from .models import ExtractedInvoice, RuleResult
from .vendor import VendorMatch


def D(v) -> Decimal:
    return Decimal(str(v))


def _fail(rule: str, exception: str, expected: str, actual: str, detail: str) -> RuleResult:
    return RuleResult(rule=rule, passed=False, exception=exception, expected=expected, actual=actual, detail=detail)


def _ok(rule: str, expected: str = "", actual: str = "") -> RuleResult:
    return RuleResult(rule=rule, passed=True, expected=expected or None, actual=actual or None)


def arithmetic(inv: ExtractedInvoice, tol_abs: float) -> list[RuleResult]:
    """Does the document agree with itself?"""
    tol = D(tol_abs)
    out = []
    for ln in inv.lines:
        if abs(ln.qty * ln.unit_price - ln.amount) > tol:
            out.append(_fail(f"line_math:{ln.line_no}", "math_error", f"{ln.qty * ln.unit_price:.2f}",
                             f"{ln.amount:.2f}", f"line {ln.line_no}: {ln.qty} x {ln.unit_price} is not {ln.amount}"))
    line_sum = sum((ln.amount for ln in inv.lines), Decimal("0"))
    expected_total = line_sum + (inv.tax or Decimal("0"))
    if inv.subtotal is not None and abs(inv.subtotal - line_sum) > tol:
        out.append(_fail("subtotal", "math_error", f"{line_sum:.2f}", f"{inv.subtotal:.2f}",
                         f"lines add up to {line_sum:.2f} but subtotal says {inv.subtotal:.2f}"))
    if abs(inv.total - expected_total) > tol:
        out.append(_fail("total", "math_error", f"{expected_total:.2f}", f"{inv.total:.2f}",
                         f"lines plus tax come to {expected_total:.2f} but the invoice asks for {inv.total:.2f}"))
    return out or [_ok("arithmetic", f"{expected_total:.2f}", f"{inv.total:.2f}")]


def three_way_match(conn: sqlite3.Connection, inv: ExtractedInvoice, vendor: VendorMatch, cfg: dict) -> list[RuleResult]:
    """Invoice vs purchase order (price) vs receipt (quantity), line by line."""
    if not inv.po_number:
        return [_fail("po_present", "missing_po", "a PO number", "none",
                      f"{vendor.name} is a PO vendor but the invoice cites no purchase order")]
    po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (inv.po_number,)).fetchone()
    if po is None:
        return [_fail("po_exists", "po_not_found", inv.po_number, "not on file",
                      f"purchase order {inv.po_number} does not exist")]
    if po["vendor_id"] != vendor.vendor_id:
        return [_fail("po_vendor", "po_vendor_mismatch", f"vendor {po['vendor_id']}", f"vendor {vendor.vendor_id}",
                      f"{inv.po_number} was issued to a different vendor")]

    price_tol = D(cfg["unit_price_tolerance_pct"])
    qty_tol = D(cfg["qty_tolerance_pct"])
    po_lines = {r["sku"]: r for r in conn.execute("SELECT * FROM po_lines WHERE po_number = ?", (inv.po_number,))}
    out = [_ok("po_exists", inv.po_number, inv.po_number)]
    billed_now: dict[str, Decimal] = {}
    for ln in inv.lines:
        pl = po_lines.get(ln.sku or "")
        if pl is None:
            out.append(_fail(f"item_on_po:{ln.sku}", "item_not_on_po", "an ordered item", str(ln.sku),
                             f"{ln.sku} ({ln.description}) was not ordered on {inv.po_number}"))
            continue
        po_price = D(pl["unit_price"])
        pct = (ln.unit_price - po_price) / po_price * 100
        if pct > price_tol:  # only an overcharge is an exception; a lower price is simply paid
            out.append(_fail(f"unit_price:{ln.sku}", "price_variance", f"{po_price:.2f}", f"{ln.unit_price:.2f}",
                             f"{ln.sku} billed at {ln.unit_price:.2f}, PO price {po_price:.2f} "
                             f"(+{pct:.1f}%, tolerance {price_tol}%), overcharge {(ln.unit_price - po_price) * ln.qty:.2f}"))
        else:
            out.append(_ok(f"unit_price:{ln.sku}", f"{po_price:.2f}", f"{ln.unit_price:.2f}"))

        billed_now[ln.sku] = billed_now.get(ln.sku, Decimal("0")) + ln.qty
        open_qty = D(pl["qty_received"]) - D(pl["qty_billed"])
        if billed_now[ln.sku] > open_qty * (1 + qty_tol / 100):
            out.append(_fail(f"qty_vs_receipt:{ln.sku}", "qty_over_receipt", f"{open_qty}", f"{billed_now[ln.sku]}",
                             f"{ln.sku}: billed {billed_now[ln.sku]}, but only {open_qty} received and not yet billed "
                             f"(ordered {D(pl['qty_ordered'])}, received {D(pl['qty_received'])}, "
                             f"already billed {D(pl['qty_billed'])})"))
        else:
            out.append(_ok(f"qty_vs_receipt:{ln.sku}", f"<= {open_qty}", f"{billed_now[ln.sku]}"))
    return out


def expense_history(conn: sqlite3.Connection, inv: ExtractedInvoice, vendor: VendorMatch, cfg: dict) -> list[RuleResult]:
    """No PO to match, so compare against what this vendor normally bills."""
    amounts = [float(r["amount"]) for r in
               conn.execute("SELECT amount FROM bill_history WHERE vendor_id = ?", (vendor.vendor_id,))]
    if len(amounts) < cfg["min_history_samples"]:
        return [_fail("history_depth", "no_history", f">= {cfg['min_history_samples']} past bills", str(len(amounts)),
                      f"only {len(amounts)} past bills from {vendor.name}; too few to judge the amount")]
    mu = mean(amounts)
    band = max(cfg["amount_sigma"] * pstdev(amounts), cfg["min_band_pct"] / 100 * mu)
    total = float(inv.total)
    expected = f"{mu - band:.2f} to {mu + band:.2f}"
    if abs(total - mu) > band:
        return [_fail("amount_vs_history", "amount_anomaly", expected, f"{total:.2f}",
                      f"{total:.2f} is {(total - mu) / mu * 100:+.0f}% against the usual {mu:.2f} from {vendor.name} "
                      f"over {len(amounts)} bills")]
    return [_ok("amount_vs_history", expected, f"{total:.2f}")]
