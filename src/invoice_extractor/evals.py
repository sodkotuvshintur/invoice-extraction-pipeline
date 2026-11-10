"""Score a pipeline run against ground truth.

Two questions, kept separate because they fail differently:

1. Extraction: did we read each field correctly?
2. Decision: did we do the right thing with the document?

The decision errors are not symmetric. Sending a good invoice to a human
costs a few minutes. Auto-passing a bad one pays money that should not have
been paid. So the headline number is the false auto-pass count, and the
touchless rate only means something next to it.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter, defaultdict
from decimal import Decimal
from pathlib import Path

FIELDS = ("vendor_name", "invoice_number", "invoice_date", "due_date", "po_number", "subtotal", "tax", "total")
MONEY = {"subtotal", "tax", "total"}


def _same(field: str, want, got) -> bool:
    if want is None or got is None:
        return want is None and got is None
    if field in MONEY:
        return Decimal(str(want)) == Decimal(str(got))
    return str(want) == str(got)


def _lines_match(want: list[dict], got: list[sqlite3.Row]) -> bool:
    if len(want) != len(got):
        return False
    return all(w["sku"] == g["sku"] and all(Decimal(str(w[k])) == Decimal(str(g[k]))
                                             for k in ("qty", "unit_price", "amount"))
               for w, g in zip(want, got))


def score(conn: sqlite3.Connection, truth_path: Path) -> dict:
    truth = json.loads(Path(truth_path).read_text())
    rows = {r["source_file"]: r for r in conn.execute("SELECT * FROM invoices")}
    exc = {r["invoice_id"]: r for r in conn.execute("SELECT * FROM exceptions")}

    field_hits: dict[str, Counter] = defaultdict(Counter)   # engine -> field -> correct
    read: Counter = Counter()                               # engine -> invoices read
    confusion: Counter = Counter()
    by_scenario: dict[str, Counter] = defaultdict(Counter)
    false_pass, needless, wrong_reason = [], [], []
    n_invoices = ceiling = touchless = 0
    stopped_value = Decimal("0")

    for t in truth:
        row = rows[t["file"]]
        got, want = row["status"], t["expected_decision"]
        got_exc = exc[row["id"]]["type"] if row["id"] in exc else None
        confusion[(want, got)] += 1
        sc = by_scenario[t["scenario"]]
        sc["n"] += 1
        sc["correct"] += got == want and got_exc == t["expected_exception"]
        total = Decimal(str(t["fields"]["total"] or 0))

        if t["is_invoice"]:
            n_invoices += 1
            ceiling += want == "auto_pass"
            touchless += got == "auto_pass"
            if row["engine"] in ("rules", "llm") and row["invoice_number"] is not None:
                engine = row["engine"]
                read[engine] += 1
                for f in FIELDS:
                    field_hits[engine][f] += _same(f, t["fields"][f], row[f])
                lines = conn.execute("SELECT * FROM invoice_lines WHERE invoice_id = ? ORDER BY line_no",
                                     (row["id"],)).fetchall()
                field_hits[engine]["lines"] += _lines_match(t["lines"], lines)

        if got == "auto_pass" and want != "auto_pass":
            false_pass.append({"file": t["file"], "scenario": t["scenario"], "total": str(total)})
        elif want == "auto_pass" and got != "auto_pass":
            needless.append({"file": t["file"], "scenario": t["scenario"], "got": got_exc})
        elif want in ("flag", "hold") and got in ("flag", "hold"):
            stopped_value += total
            if got_exc != t["expected_exception"]:
                wrong_reason.append({"file": t["file"], "want": t["expected_exception"], "got": got_exc})

    n = len(truth)
    return {
        "documents": n,
        "invoices": n_invoices,
        "decision_accuracy": sum(v for (w, g), v in confusion.items() if w == g) / n,
        "touchless_rate": touchless / n_invoices,
        "touchless_ceiling": ceiling / n_invoices,
        "false_auto_pass": false_pass,
        "needless_reviews": needless,
        "wrong_exception_type": wrong_reason,
        "problem_value_stopped": str(stopped_value),
        "confusion": {f"{w} -> {g}": v for (w, g), v in sorted(confusion.items())},
        "by_scenario": {s: dict(c) for s, c in sorted(by_scenario.items())},
        "extraction": {e: {"read": read[e], **{f: field_hits[e][f] / read[e] for f in (*FIELDS, "lines")}}
                       for e in read},
        "queues": {r["queue"]: r["k"] for r in conn.execute(
            "SELECT queue, COUNT(*) k FROM exceptions GROUP BY queue ORDER BY k DESC")},
        "engines": {r["engine"]: r["k"] for r in conn.execute(
            "SELECT engine, COUNT(*) k FROM invoices GROUP BY engine")},
    }


def to_markdown(s: dict, title: str = "Evaluation report") -> str:
    pct = lambda v: f"{v:.1%}"  # noqa: E731
    out = [f"# {title}", "",
           f"{s['documents']} documents, {s['invoices']} of them invoices. "
           f"Engines used: {', '.join(f'{e} ({k})' for e, k in s['engines'].items())}.", "",
           "## Decisions", "",
           "| Metric | Value |", "|---|---|",
           f"| **False auto-pass** (bad invoice queued for payment) | **{len(s['false_auto_pass'])}** |",
           f"| Touchless rate (invoices auto-passed) | {pct(s['touchless_rate'])} |",
           f"| Touchless ceiling (invoices that *should* auto-pass) | {pct(s['touchless_ceiling'])} |",
           f"| Needless reviews (good invoice sent to a human) | {len(s['needless_reviews'])} |",
           f"| Decision accuracy | {pct(s['decision_accuracy'])} |",
           f"| Right decision, wrong exception type | {len(s['wrong_exception_type'])} |",
           f"| Value of problem invoices stopped | ${Decimal(s['problem_value_stopped']):,.2f} |", "",
           "### Expected -> actual", "", "| Expected -> actual | Documents |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in s["confusion"].items()]
    out += ["", "### By scenario", "", "| Scenario | Documents | Fully correct |", "|---|---|---|"]
    out += [f"| {k} | {v['n']} | {v.get('correct', 0)} |" for k, v in s["by_scenario"].items()]
    if s["needless_reviews"]:
        reasons = Counter(f"{r['scenario']} -> {r['got']}" for r in s["needless_reviews"])
        out += ["", "### Needless reviews by cause", "", "| Scenario -> exception raised | Documents |", "|---|---|"]
        out += [f"| {k} | {v} |" for k, v in reasons.most_common()]
    out += ["", "## Extraction accuracy", "", "Share of invoices the engine read where the field exactly "
            "matches ground truth. `lines` requires every line's code, quantity, price and amount to match.", ""]
    cols = (*FIELDS, "lines")
    out += ["| Engine | Read | " + " | ".join(cols) + " |", "|---|---|" + "---|" * len(cols)]
    for engine, e in s["extraction"].items():
        out.append(f"| {engine} | {e['read']} | " + " | ".join(pct(e[c]) for c in cols) + " |")
    out += ["", "## Work queues", "", "| Queue | Open exceptions |", "|---|---|"]
    out += [f"| {k} | {v} |" for k, v in s["queues"].items()]
    return "\n".join(out) + "\n"
