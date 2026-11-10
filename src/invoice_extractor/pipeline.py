"""One document in, one decision out, everything written down.

    ingest -> extract -> gate -> resolve vendor -> duplicate check
           -> arithmetic -> rulebook (3-way match or expense history) -> decide

The first failing check names the exception and the queue that owns it.
AUTO_PASS writes a ledger-outbox row; nothing here posts to a ledger directly.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from importlib import resources
from pathlib import Path
from typing import Optional

from . import validation
from .extraction.composite import Engine
from .models import KEY_FIELDS, Decision, Extraction, Outcome, RuleResult
from .vendor import VendorMatch, resolve


def load_config(path: Optional[str | Path] = None) -> dict:
    if path:
        return json.loads(Path(path).read_text())
    return json.loads(resources.files("invoice_extractor").joinpath("tolerances.json").read_text())


def _fail(rule: str, exception: str, detail: str, expected: str = "", actual: str = "") -> RuleResult:
    return RuleResult(rule=rule, passed=False, exception=exception, detail=detail,
                      expected=expected or None, actual=actual or None)


def _check(conn: sqlite3.Connection, invoice_id: int, content_hash: str, ext: Extraction,
           cfg: dict) -> tuple[list[RuleResult], Optional[VendorMatch]]:
    """Run the checks in order of how fundamental they are; stop where later ones would be meaningless."""
    inv = ext.invoice
    threshold = cfg["auto_pass_min_confidence"]
    results: list[RuleResult] = []

    missing = [f for f in KEY_FIELDS if not getattr(inv, f)]
    if missing:
        return [_fail("complete", "incomplete", f"could not read: {', '.join(missing)}")], None
    results.append(RuleResult(rule="complete", passed=True))

    conf = ext.overall_confidence
    if conf < threshold:
        weak = sorted(f for f in KEY_FIELDS if ext.field_confidence.get(f, 0) < threshold)
        return results + [_fail("confidence", "low_confidence",
                                f"extraction confidence {conf:.2f} is below {threshold:.2f} on: {', '.join(weak)}",
                                f">= {threshold:.2f}", f"{conf:.2f}")], None
    results.append(RuleResult(rule="confidence", passed=True, expected=f">= {threshold:.2f}", actual=f"{conf:.2f}"))

    vendor = resolve(conn, inv.vendor_name, cfg["vendor_match_min_score"])
    if vendor is None:
        return results + [_fail("vendor_match", "vendor_unknown",
                                f"'{inv.vendor_name}' does not match any active vendor", "a known vendor",
                                inv.vendor_name)], None
    results.append(RuleResult(rule="vendor_match", passed=True, expected=vendor.name,
                              actual=f"{inv.vendor_name} (score {vendor.score})"))

    # A resend is caught two ways: the same bytes, or the same number from the same vendor.
    prior = conn.execute(
        "SELECT id, source_file FROM invoices WHERE id < ? AND status != 'not_applicable' "
        "AND (content_hash = ? OR (vendor_id = ? AND invoice_number = ?)) ORDER BY id LIMIT 1",
        (invoice_id, content_hash, vendor.vendor_id, inv.invoice_number)).fetchone()
    if prior:
        return results + [_fail("duplicate", "duplicate",
                                f"invoice {inv.invoice_number} from {vendor.name} was already received "
                                f"as {prior['source_file']}", "first submission",
                                f"repeat of invoice #{prior['id']}")], vendor
    results.append(RuleResult(rule="duplicate", passed=True))

    results += validation.arithmetic(inv, cfg["arithmetic_tolerance_abs"])
    if vendor.category == "inventory":
        results += validation.three_way_match(conn, inv, vendor, cfg["inventory"])
    else:
        results += validation.expense_history(conn, inv, vendor, cfg["expense"])
    return results, vendor


def _decide(results: list[RuleResult], cfg: dict) -> Outcome:
    failed = [r for r in results if not r.passed]
    if not failed:
        return Outcome(decision=Decision.AUTO_PASS, reason="all checks passed", results=results)
    first = failed[0]
    decision = Decision.HOLD if first.exception == "duplicate" else Decision.FLAG
    more = f" (+{len(failed) - 1} more)" if len(failed) > 1 else ""
    return Outcome(decision=decision, exception=first.exception, queue=cfg["queues"][first.exception],
                   reason=(first.detail or first.rule) + more, results=results)


def process(conn: sqlite3.Connection, path: Path, extractor: Engine, cfg: dict) -> Outcome:
    data = Path(path).read_bytes()
    content_hash = hashlib.sha256(data).hexdigest()
    invoice_id = conn.execute("INSERT INTO invoices (source_file, content_hash) VALUES (?, ?)",
                              (Path(path).name, content_hash)).lastrowid

    ext = extractor.extract(data)
    conn.execute("INSERT INTO extractions (invoice_id, engine, raw_json, field_conf_json) VALUES (?, ?, ?, ?)",
                 (invoice_id, ext.engine, ext.invoice.model_dump_json() if ext.invoice else "null",
                  json.dumps(ext.field_confidence)))
    inv, vendor = ext.invoice, None

    if inv is None:
        outcome = Outcome(decision=Decision.FLAG, exception="extraction_failed",
                          queue=cfg["queues"]["extraction_failed"],
                          reason="; ".join(ext.notes) or "document could not be read")
    elif not inv.is_invoice:
        if ext.overall_confidence >= cfg["auto_pass_min_confidence"]:
            outcome = Outcome(decision=Decision.NOT_APPLICABLE, reason="; ".join(ext.notes) or "not an invoice")
        else:  # dismissing a real bill is the one error nobody would review, so a person confirms
            outcome = Outcome(decision=Decision.FLAG, exception="low_confidence",
                              queue=cfg["queues"]["low_confidence"],
                              reason="read as 'not an invoice' without enough certainty to dismiss it")
    else:
        results, vendor = _check(conn, invoice_id, content_hash, ext, cfg)
        outcome = _decide(results, cfg)

    _persist(conn, invoice_id, ext, vendor, outcome)
    conn.commit()
    return outcome


def _persist(conn: sqlite3.Connection, invoice_id: int, ext: Extraction, vendor: Optional[VendorMatch],
             outcome: Outcome) -> None:
    inv = ext.invoice
    s = lambda v: None if v is None else str(v)  # noqa: E731
    if inv:
        conn.execute(
            "UPDATE invoices SET vendor_id=?, vendor_name=?, invoice_number=?, invoice_date=?, due_date=?, "
            "po_number=?, subtotal=?, tax=?, total=?, category=? WHERE id=?",
            (vendor.vendor_id if vendor else None, inv.vendor_name, inv.invoice_number, s(inv.invoice_date),
             s(inv.due_date), inv.po_number, s(inv.subtotal), s(inv.tax), s(inv.total),
             vendor.category if vendor else None, invoice_id))
        conn.executemany(
            "INSERT INTO invoice_lines VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(invoice_id, ln.line_no, ln.sku, ln.description, str(ln.qty), str(ln.unit_price), str(ln.amount))
             for ln in inv.lines])
    conn.execute("UPDATE invoices SET engine=?, confidence=?, status=? WHERE id=?",
                 (ext.engine, round(ext.overall_confidence, 4), outcome.decision.value, invoice_id))
    conn.executemany(
        "INSERT INTO validations (invoice_id, rule, passed, expected, actual, detail) VALUES (?, ?, ?, ?, ?, ?)",
        [(invoice_id, r.rule, int(r.passed), r.expected, r.actual, r.detail) for r in outcome.results])

    if outcome.decision in (Decision.FLAG, Decision.HOLD):
        conn.execute("INSERT INTO exceptions (invoice_id, type, queue, reason) VALUES (?, ?, ?, ?)",
                     (invoice_id, outcome.exception, outcome.queue, outcome.reason))
    elif outcome.decision is Decision.AUTO_PASS:
        payload = {"vendor_id": vendor.vendor_id, "vendor": vendor.name, "invoice_number": inv.invoice_number,
                   "invoice_date": s(inv.invoice_date), "due_date": s(inv.due_date), "po_number": inv.po_number,
                   "total": s(inv.total),
                   "lines": [{"sku": l.sku, "qty": str(l.qty), "amount": str(l.amount)} for l in inv.lines]}
        conn.execute(
            "INSERT INTO ledger_outbox (invoice_id, intent, payload_json, idempotency_ref) VALUES (?, 'bill_create', ?, ?)",
            (invoice_id, json.dumps(payload), f"bill:{vendor.vendor_id}:{inv.invoice_number}"))
        if inv.po_number:  # consume the receipt so the same goods cannot be billed twice
            for ln in inv.lines:
                conn.execute("UPDATE po_lines SET qty_billed = qty_billed + ? WHERE po_number = ? AND sku = ?",
                             (str(ln.qty), inv.po_number, ln.sku))

    conn.execute(
        "INSERT INTO audit_log (invoice_id, from_state, to_state, actor, detail_json) VALUES (?, 'ingested', ?, ?, ?)",
        (invoice_id, outcome.decision.value, f"pipeline/{ext.engine}",
         json.dumps({"exception": outcome.exception, "queue": outcome.queue, "reason": outcome.reason,
                     "confidence": round(ext.overall_confidence, 4), "notes": ext.notes})))


def run_folder(conn: sqlite3.Connection, folder: Path, extractor: Engine, cfg: dict) -> list[tuple[str, Outcome]]:
    """Process every PDF in arrival (filename) order."""
    return [(p.name, process(conn, p, extractor, cfg)) for p in sorted(Path(folder).glob("*.pdf"))]
