"""Command line: generate -> run -> eval, or `demo` for all three."""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from pathlib import Path

from . import db, evals, pipeline
from .extraction.composite import CompositeExtractor
from .synth.generate import generate


def _extractor(use_llm: bool) -> CompositeExtractor:
    if not use_llm:
        return CompositeExtractor()
    from .extraction.llm import LlmExtractor
    return CompositeExtractor(llm=LlmExtractor())


def cmd_generate(a) -> int:
    info = generate(Path(a.data), n=a.n, seed=a.seed)
    print(f"generated {info['documents']} documents and {info['purchase_orders']} purchase orders in {a.data}/")
    return 0


def cmd_run(a) -> int:
    data = Path(a.data)
    conn = db.fresh(data / "ap.db", data / "master_data.json")
    outcomes = pipeline.run_folder(conn, data / "invoices", _extractor(a.llm), pipeline.load_config(a.config))
    tally = Counter(o.decision.value for _, o in outcomes)
    print(f"processed {len(outcomes)} documents: " + ", ".join(f"{k} {v}" for k, v in tally.most_common()))
    if a.verbose:
        for name, o in outcomes:
            print(f"  {name}  {o.decision.value:<14} {o.queue or '':<18} {o.reason}")
    return 0


def cmd_eval(a) -> int:
    data = Path(a.data)
    summary = evals.score(db.connect(data / "ap.db"), data / "ground_truth.json")
    report = evals.to_markdown(summary)
    print(report)
    if a.report:
        Path(a.report).parent.mkdir(parents=True, exist_ok=True)
        Path(a.report).write_text(report, encoding="utf-8")
    if summary["false_auto_pass"]:
        print(f"FAIL: {len(summary['false_auto_pass'])} bad invoice(s) auto-passed", file=sys.stderr)
        return 1
    return 0


def cmd_queue(a) -> int:
    conn = db.connect(Path(a.data) / "ap.db")
    rows = conn.execute(
        "SELECT e.queue, e.type, i.source_file, i.vendor_name, i.invoice_number, i.total, e.reason "
        "FROM exceptions e JOIN invoices i ON i.id = e.invoice_id WHERE e.state = 'open' "
        "ORDER BY e.queue, e.type, i.id").fetchall()
    queue = None
    for r in rows:
        if r["queue"] != queue:
            queue = r["queue"]
            print(f"\n== {queue} ==")
        total = f"${float(r['total']):,.2f}" if r["total"] is not None else "-"
        print(f"  [{r['type']}] {r['source_file']}  {r['vendor_name'] or '?'}  #{r['invoice_number'] or '?'}  {total}")
        print(f"      {r['reason']}")
    return 0


def cmd_demo(a) -> int:
    return cmd_generate(a) or cmd_run(a) or cmd_eval(a)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="invoice-extractor", description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp, *, gen=False, run=False, ev=False):
        sp.add_argument("--data", default="data", help="working folder (default: data)")
        if gen:
            sp.add_argument("--n", type=int, default=120, help="approximate number of documents")
            sp.add_argument("--seed", type=int, default=7)
        if run:
            sp.add_argument("--llm", action="store_true",
                            help="send documents no template recognises to Claude (needs Anthropic credentials)")
            sp.add_argument("--config", help="path to a tolerances.json override")
            sp.add_argument("-v", "--verbose", action="store_true", help="print one line per document")
        if ev:
            sp.add_argument("--report", help="also write the report to this markdown file")

    for name, fn, flags in (
        ("generate", cmd_generate, dict(gen=True)), ("run", cmd_run, dict(run=True)),
        ("eval", cmd_eval, dict(ev=True)), ("queue", cmd_queue, {}),
        ("demo", cmd_demo, dict(gen=True, run=True, ev=True)),
    ):
        sp = sub.add_parser(name)
        common(sp, **flags)
        sp.set_defaults(fn=fn)
    a = p.parse_args(argv)
    return a.fn(a)
