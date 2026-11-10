"""Template parsers against generated PDFs, and the LLM engine against a scripted client."""
import json
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from invoice_extractor.extraction import rules
from invoice_extractor.extraction.composite import CompositeExtractor
from invoice_extractor.extraction.llm import LlmExtractor, LlmInvoice, LlmLine
from invoice_extractor.synth.generate import generate


@pytest.fixture(scope="module")
def world(tmp_path_factory):
    out = tmp_path_factory.mktemp("data")
    generate(out, n=60, seed=3)
    return out, json.loads((out / "ground_truth.json").read_text())


def _first(world, **match):
    out, truth = world
    doc = next(t for t in truth if all(t[k] == v for k, v in match.items()))
    return (out / "invoices" / doc["file"]).read_bytes(), doc


@pytest.mark.parametrize("layout", ["A", "B", "C"])
def test_template_reads_every_field_of_its_layout(world, layout):
    pdf, truth = _first(world, layout=layout, scenario="clean")
    ext = rules.extract(pdf)
    inv, want = ext.invoice, truth["fields"]
    assert inv.vendor_name == want["vendor_name"]
    assert inv.invoice_number == want["invoice_number"]
    assert inv.invoice_date == date.fromisoformat(want["invoice_date"])
    assert inv.due_date == date.fromisoformat(want["due_date"])
    assert inv.po_number == want["po_number"]
    assert inv.total == Decimal(want["total"])
    assert [(ln.sku, ln.qty, ln.unit_price, ln.amount) for ln in inv.lines] == \
           [(ln["sku"], Decimal(ln["qty"]), Decimal(ln["unit_price"]), Decimal(ln["amount"])) for ln in truth["lines"]]
    assert ext.overall_confidence >= 0.9


def test_missing_po_is_read_as_absent_not_invented(world):
    pdf, _ = _first(world, scenario="missing_po")
    assert rules.extract(pdf).invoice.po_number is None


def test_statement_is_recognised_as_not_an_invoice(world):
    pdf, _ = _first(world, scenario="statement")
    assert rules.extract(pdf).invoice.is_invoice is False


def test_templates_decline_a_layout_they_do_not_know(world):
    pdf, _ = _first(world, layout="D")
    assert rules.extract(pdf) is None
    assert CompositeExtractor().extract(pdf).ok is False      # no LLM configured: unread, not guessed


def test_generation_is_reproducible(tmp_path):
    generate(tmp_path / "a", n=30, seed=11)
    generate(tmp_path / "b", n=30, seed=11)
    assert (tmp_path / "a/ground_truth.json").read_text() == (tmp_path / "b/ground_truth.json").read_text()
    assert (tmp_path / "a/invoices/doc_0001.pdf").read_bytes() == (tmp_path / "b/invoices/doc_0001.pdf").read_bytes()


# ---- LLM engine, with the API client scripted ----

class FakeClient:
    """Stands in for anthropic.Anthropic: records the request, returns a canned parse."""

    def __init__(self, parsed, stop_reason="end_turn"):
        self.request = None
        self._response = SimpleNamespace(parsed_output=parsed, stop_reason=stop_reason, model="fake-model")
        self.beta = SimpleNamespace(messages=SimpleNamespace(parse=self._parse))

    def _parse(self, **kwargs):
        self.request = kwargs
        return self._response


def _as_llm(truth, **override) -> LlmInvoice:
    f = truth["fields"]
    base = dict(is_invoice=True, vendor_name=f["vendor_name"], invoice_number=f["invoice_number"],
                invoice_date=f["invoice_date"], due_date=f["due_date"], po_number=f["po_number"],
                subtotal=None, tax=None, total=f["total"],
                lines=[LlmLine(sku=ln["sku"], description=ln["description"], qty=ln["qty"],
                               unit_price=ln["unit_price"], amount=ln["amount"]) for ln in truth["lines"]])
    return LlmInvoice(**{**base, **override})


def test_llm_engine_reads_a_freeform_invoice_and_grounds_it(world):
    pdf, truth = _first(world, layout="D")
    client = FakeClient(_as_llm(truth))
    ext = CompositeExtractor(llm=LlmExtractor(client=client)).extract(pdf)
    assert ext.engine == "llm" and ext.invoice.total == Decimal(truth["fields"]["total"])
    assert ext.invoice.subtotal == ext.invoice.total            # derived from the lines
    assert ext.overall_confidence >= 0.9                        # every key value found in the PDF text
    sent = client.request
    assert sent["output_format"] is LlmInvoice
    assert sent["messages"][0]["content"][0]["source"]["media_type"] == "application/pdf"


def test_llm_is_not_called_when_a_template_matches(world):
    pdf, _ = _first(world, layout="A", scenario="clean")
    client = FakeClient(None)
    assert CompositeExtractor(llm=LlmExtractor(client=client)).extract(pdf).engine == "rules"
    assert client.request is None


def test_a_total_that_is_not_in_the_document_scores_low(world):
    pdf, truth = _first(world, layout="D")
    ext = LlmExtractor(client=FakeClient(_as_llm(truth, total="987654.32"))).extract(pdf)
    assert ext.field_confidence["total"] < 0.9 and ext.overall_confidence < 0.9


def test_an_invoice_number_that_is_not_in_the_document_scores_low(world):
    pdf, truth = _first(world, layout="D")
    ext = LlmExtractor(client=FakeClient(_as_llm(truth, invoice_number="ZZ-00000"))).extract(pdf)
    assert ext.field_confidence["invoice_number"] < 0.9


def test_llm_saying_not_an_invoice_is_capped_below_the_dismiss_threshold(world):
    pdf, truth = _first(world, layout="D")
    ext = LlmExtractor(client=FakeClient(_as_llm(truth, is_invoice=False))).extract(pdf)
    assert ext.overall_confidence < 0.9


def test_refusal_or_truncation_is_a_failed_read_not_a_partial_one(world):
    pdf, truth = _first(world, layout="D")
    for stop in ("refusal", "max_tokens"):
        ext = LlmExtractor(client=FakeClient(_as_llm(truth), stop_reason=stop)).extract(pdf)
        assert ext.ok is False and stop in ext.notes[0]
