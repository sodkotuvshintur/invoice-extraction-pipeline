"""Generate a world, run it, score it. This is the safety property CI enforces."""
from invoice_extractor import db, evals, pipeline
from invoice_extractor.extraction.composite import CompositeExtractor
from invoice_extractor.synth.generate import generate
from invoice_extractor.vendor import normalise


def test_no_bad_invoice_is_ever_auto_passed(tmp_path):
    for seed in (1, 2, 3):
        out = tmp_path / str(seed)
        generate(out, n=80, seed=seed)
        conn = db.fresh(":memory:", out / "master_data.json")
        pipeline.run_folder(conn, out / "invoices", CompositeExtractor(), pipeline.load_config())
        s = evals.score(conn, out / "ground_truth.json")

        assert s["false_auto_pass"] == []
        assert s["wrong_exception_type"] == []
        # without an LLM the only good invoices held back are the layouts no template reads
        assert {r["scenario"] for r in s["needless_reviews"]} <= {"freeform"}
        assert all(v == 1.0 for k, v in s["extraction"]["rules"].items() if k != "read")


def test_vendor_name_normalisation():
    assert normalise("CASCADE FASTENER COMPANY, INC.") == normalise("Cascade Fastener Co.")
    assert normalise("Redrock Concrete and Masonry") == normalise("Redrock Concrete & Masonry")
