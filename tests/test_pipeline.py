"""The decision rules, one behaviour per test."""
import json

from conftest import HIGH, invoice
from invoice_extractor.models import Decision, ExtractedInvoice


def test_clean_invoice_auto_passes_and_queues_one_bill(conn, submit):
    out = submit(invoice())
    assert out.decision is Decision.AUTO_PASS
    row = conn.execute("SELECT intent, idempotency_ref, payload_json FROM ledger_outbox").fetchone()
    assert (row["intent"], row["idempotency_ref"]) == ("bill_create", "bill:1:INV-1")
    assert json.loads(row["payload_json"])["total"] == "200.00"
    assert conn.execute("SELECT COUNT(*) FROM exceptions").fetchone()[0] == 0


def test_printed_name_resolves_to_master_vendor(conn, submit):
    assert submit(invoice(vendor="CASCADE FASTENER COMPANY, INC.")).decision is Decision.AUTO_PASS
    assert conn.execute("SELECT vendor_id FROM invoices").fetchone()[0] == 1


def test_unknown_vendor_goes_to_onboarding(submit):
    out = submit(invoice(vendor="Apex Industrial Abrasives"))
    assert (out.decision, out.exception, out.queue) == (Decision.FLAG, "vendor_unknown", "vendor_onboarding")


def test_price_within_tolerance_passes(submit):
    assert submit(invoice(lines=(("FST-1", 10, "20.30"),))).decision is Decision.AUTO_PASS  # +1.5%, limit 2%


def test_price_over_tolerance_flags_purchasing_with_the_overcharge(submit):
    out = submit(invoice(lines=(("FST-1", 10, "21.00"),)))                                # +5%
    assert (out.decision, out.exception, out.queue) == (Decision.FLAG, "price_variance", "purchasing")
    assert "overcharge 10.00" in out.reason


def test_lower_price_than_po_is_not_an_exception(submit):
    assert submit(invoice(lines=(("FST-1", 10, "18.00"),))).decision is Decision.AUTO_PASS


def test_billing_more_than_received_flags_receiving(submit):
    out = submit(invoice(lines=(("FST-2", 4, "5.00"),)))     # 4 ordered, only 2 received
    assert (out.exception, out.queue) == ("qty_over_receipt", "receiving")


def test_same_invoice_number_is_held_as_duplicate(conn, submit):
    assert submit(invoice()).decision is Decision.AUTO_PASS
    out = submit(invoice())
    assert (out.decision, out.exception) == (Decision.HOLD, "duplicate")
    assert conn.execute("SELECT COUNT(*) FROM ledger_outbox").fetchone()[0] == 1


def test_identical_bytes_are_held_even_if_the_number_reads_differently(submit):
    assert submit(invoice(), content=b"same file").decision is Decision.AUTO_PASS
    assert submit(invoice(number="INV-l"), content=b"same file").exception == "duplicate"


def test_renumbered_resend_is_caught_because_the_receipt_is_already_billed(submit):
    """A new invoice number defeats a number check. The receipt has no quantity left to bill."""
    assert submit(invoice(number="INV-1")).decision is Decision.AUTO_PASS
    out = submit(invoice(number="INV-1-A"))
    assert (out.decision, out.exception) == (Decision.FLAG, "qty_over_receipt")
    assert "already billed 10" in out.reason


def test_partial_billing_is_allowed_up_to_the_received_quantity(submit):
    assert submit(invoice(number="A", lines=(("FST-1", 6, "20.00"),))).decision is Decision.AUTO_PASS
    assert submit(invoice(number="B", lines=(("FST-1", 4, "20.00"),))).decision is Decision.AUTO_PASS
    assert submit(invoice(number="C", lines=(("FST-1", 1, "20.00"),))).exception == "qty_over_receipt"


def test_flagged_invoice_does_not_consume_the_receipt(conn, submit):
    submit(invoice(lines=(("FST-1", 10, "25.00"),)))         # price variance, flagged
    assert conn.execute("SELECT qty_billed FROM po_lines WHERE sku = 'FST-1'").fetchone()[0] == 0


def test_missing_po_on_a_po_vendor(submit):
    out = submit(invoice(po=None))
    assert (out.exception, out.queue) == ("missing_po", "purchasing")


def test_po_that_does_not_exist(submit):
    assert submit(invoice(po="PO-999")).exception == "po_not_found"


def test_po_issued_to_another_vendor(submit):
    assert submit(invoice(po="PO-2")).exception == "po_vendor_mismatch"


def test_item_that_was_never_ordered(submit):
    assert submit(invoice(lines=(("FST-9", 1, "20.00"),))).exception == "item_not_on_po"


def test_total_that_does_not_add_up(submit):
    out = submit(invoice(total="300.00"))                    # lines come to 200.00
    assert (out.exception, out.queue) == ("math_error", "ap_review")


def test_expense_in_line_with_history_passes(submit):
    inv = invoice(vendor="Metro Power & Light", po=None, lines=(("UTL-1", 1, "1010.00"),))
    assert submit(inv).decision is Decision.AUTO_PASS


def test_expense_spike_flags_accounting(submit):
    inv = invoice(vendor="Metro Power & Light", po=None, lines=(("UTL-1", 1, "1700.00"),))
    out = submit(inv)
    assert (out.exception, out.queue) == ("amount_anomaly", "accounting")


def test_expense_vendor_without_enough_history_is_not_auto_passed(conn, submit):
    conn.execute("DELETE FROM bill_history WHERE id > 2")
    inv = invoice(vendor="Metro Power & Light", po=None, lines=(("UTL-1", 1, "1010.00"),))
    assert submit(inv).exception == "no_history"


def test_low_confidence_read_never_auto_passes_even_if_everything_matches(submit):
    out = submit(invoice(), confidence={**HIGH, "total": 0.5})
    assert (out.decision, out.exception) == (Decision.FLAG, "low_confidence")
    assert "total" in out.reason


def test_unreadable_field_is_incomplete(submit):
    inv = invoice()
    inv.invoice_number = None
    assert submit(inv).exception == "incomplete"


def test_statement_is_recorded_and_ignored(conn, submit):
    out = submit(ExtractedInvoice(is_invoice=False, vendor_name="Cascade Fastener Co."))
    assert out.decision is Decision.NOT_APPLICABLE
    assert conn.execute("SELECT COUNT(*) FROM exceptions").fetchone()[0] == 0


def test_uncertain_not_an_invoice_is_reviewed_rather_than_dropped(submit):
    out = submit(ExtractedInvoice(is_invoice=False), confidence={"is_invoice": 0.8}, engine_name="llm")
    assert (out.decision, out.exception) == (Decision.FLAG, "low_confidence")


def test_every_decision_leaves_evidence(conn, submit):
    submit(invoice(lines=(("FST-1", 10, "21.00"),)))
    rules = {r["rule"]: r["passed"] for r in conn.execute("SELECT rule, passed FROM validations")}
    assert rules["vendor_match"] == 1 and rules["unit_price:FST-1"] == 0 and rules["qty_vs_receipt:FST-1"] == 1
    audit = conn.execute("SELECT to_state, actor, detail_json FROM audit_log").fetchone()
    assert (audit["to_state"], audit["actor"]) == ("flag", "pipeline/rules")
    assert json.loads(audit["detail_json"])["exception"] == "price_variance"
    assert conn.execute("SELECT COUNT(*) FROM extractions").fetchone()[0] == 1
