"""Generate a synthetic AP world: master data, invoice PDFs, and ground truth.

Everything is invented. The point is a dataset where the *right answer is
known* for every document -- both what the fields are and what AP should do
with it -- so extraction and decisions can be scored instead of eyeballed.

Each document is assigned a scenario. The scenario fixes the expected
decision (auto-pass, flag to a named queue, hold, or ignore), which is what a
careful AP clerk with full access to the PO and receiving records would do.
"""
from __future__ import annotations

import json
import random
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

from . import layouts

CENT = Decimal("0.01")


def q(v) -> Decimal:
    return Decimal(str(v)).quantize(CENT, rounding=ROUND_HALF_UP)


# (id, master name, name as printed on its invoices, category, layout, sku prefix, invoice-number style)
VENDORS = [
    (1, "Cascade Fastener Co.", "CASCADE FASTENER COMPANY, INC.", "inventory", "A", "FST", "INV-{n}"),
    (2, "Timberline Lumber Supply", "Timberline Lumber Supply LLC", "inventory", "B", "LBR", "{n}"),
    (3, "Ironbridge Hardware Wholesale", "Ironbridge Hardware Wholesale", "inventory", "C", "HDW", "IB/2026/{n}"),
    (4, "Summit Roofing Materials", "Summit Roofing Materials Corp.", "inventory", "A", "ROF", "SRM-{n}"),
    (5, "Bluewater Plumbing Supply", "Blue Water Plumbing Supply Co", "inventory", "B", "PLB", "{n}"),
    (6, "Redrock Concrete & Masonry", "Redrock Concrete and Masonry", "inventory", "C", "MAS", "RR/{n}"),
    (7, "Keystone Electrical Distributors", "Keystone Electrical Distributors", "inventory", "A", "ELC", "KE-{n}"),
    (8, "Lakeshore Paint & Coatings", "Lakeshore Paint & Coatings, LLC", "inventory", "B", "PNT", "{n}"),
    (9, "Old Mill Reclaimed Wood", "Old Mill Reclaimed Wood", "inventory", "D", "RWD", "{n}"),
    (10, "Hollis & Daughters Millwork", "Hollis & Daughters Millwork", "inventory", "D", "MLW", "{n}"),
    (11, "Metro Power & Light", "Metro Power & Light", "expense", "A", "UTL", "MPL-{n}"),
    (12, "ClearPath Waste Services", "ClearPath Waste Services Inc.", "expense", "B", "SVC", "{n}"),
    (13, "Northgate Property Management", "Northgate Property Management", "expense", "C", "RNT", "NG/{n}"),
    (14, "Brightline Telecom", "Brightline Telecom", "expense", "A", "TEL", "BT-{n}"),
]
UNKNOWN_VENDORS = [("Apex Industrial Abrasives", "A", "ABR"), ("Granite State Tool Rental", "B", "RNL")]

ITEMS = {
    "FST": [("Deck screw #8 x 2-1/2in, 5lb box", 24.50), ("Lag bolt 3/8 x 4in, box of 50", 31.75),
            ("Joist hanger 2x8 galvanized", 3.10), ("Framing nail 16d, 50lb carton", 68.00)],
    "LBR": [("2x4 SPF stud 8ft", 4.85), ("2x6 SPF 12ft", 11.40), ("Plywood CDX 1/2in 4x8", 38.90),
            ("Pressure treated 4x4 8ft", 17.25)],
    "HDW": [("Door hinge 3-1/2in satin nickel", 5.60), ("Deadbolt single cylinder", 27.80),
            ("Cabinet pull 96mm matte black", 3.95), ("Barn door track kit 6ft", 89.00)],
    "ROF": [("Architectural shingle bundle", 36.40), ("Roofing felt 30lb roll", 29.90),
            ("Drip edge 10ft aluminum", 8.75), ("Ridge vent 4ft section", 14.20)],
    "PLB": [("PEX pipe 1/2in 100ft coil", 42.00), ("Copper elbow 3/4in", 3.45),
            ("Ball valve 1/2in brass", 11.90), ("PVC cement 16oz", 9.30)],
    "MAS": [("Concrete mix 80lb bag", 6.85), ("Mortar mix type S 60lb", 8.40),
            ("Concrete block 8x8x16", 3.20), ("Rebar #4 20ft", 12.60)],
    "ELC": [("Romex 12/2 NM-B 250ft", 118.00), ("Duplex receptacle 15A, box of 10", 13.50),
            ("Breaker 20A single pole", 7.90), ("LED shop light 4ft", 32.00)],
    "PNT": [("Interior latex eggshell 1gal", 34.75), ("Exterior primer 5gal", 112.00),
            ("Deck stain semi-transparent 1gal", 41.50), ("Painter's tape 1.88in, 6-pack", 22.40)],
    "RWD": [("Reclaimed oak beam 6x6 8ft", 185.00), ("Barn board siding, per bundle", 96.00),
            ("Heart pine flooring, per carton", 142.50)],
    "MLW": [("Custom oak stair tread 42in", 64.00), ("Fluted door casing set", 58.50),
            ("Crown moulding 5-1/4in 16ft", 37.25)],
    "ABR": [("Flap disc 4-1/2in 60 grit, box of 10", 28.00), ("Cut-off wheel 14in", 9.60)],
    "RNL": [("Plate compactor rental, weekly", 310.00), ("Scaffold tower rental, weekly", 185.00)],
}
EXPENSE = {  # vendor_id -> (line description, typical monthly amount, tax rate)
    11: ("Electric service - warehouse", 2840.00, 0.0),
    12: ("Waste collection - 8yd container", 615.00, 0.065),
    13: ("Monthly rent - unit 4B", 7200.00, 0.0),
    14: ("Fiber internet and voice", 489.00, 0.0),
}

# share of the document set per scenario; whatever is left over is clean inventory
MIX = {
    "statement": 0.04, "expense_normal": 0.12, "expense_spike": 0.04, "freeform": 0.07,
    "price_within_tolerance": 0.06, "price_variance": 0.07, "qty_over_receipt": 0.06,
    "duplicate": 0.05, "resubmitted_new_number": 0.03, "unknown_vendor": 0.04,
    "missing_po": 0.04, "math_error": 0.03,
}
# scenario -> (expected decision, expected exception type)
EXPECTED = {
    "clean": ("auto_pass", None), "expense_normal": ("auto_pass", None), "freeform": ("auto_pass", None),
    "price_within_tolerance": ("auto_pass", None),
    "price_variance": ("flag", "price_variance"), "qty_over_receipt": ("flag", "qty_over_receipt"),
    "resubmitted_new_number": ("flag", "qty_over_receipt"),
    "duplicate": ("hold", "duplicate"), "unknown_vendor": ("flag", "vendor_unknown"),
    "missing_po": ("flag", "missing_po"), "math_error": ("flag", "math_error"),
    "expense_spike": ("flag", "amount_anomaly"), "statement": ("not_applicable", None),
}


class _World:
    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.vendors = {v[0]: v for v in VENDORS}
        self.pos: list[dict] = []
        self.history: list[dict] = []
        self.docs: list[dict] = []
        self._po_seq = 20400
        self._inv_seq = {v[0]: self.rng.randint(1000, 8000) for v in VENDORS}
        self.start = date(2026, 8, 3)

    # ---- building blocks ----
    def _addr(self) -> str:
        r = self.rng
        street = r.choice(["Foundry Rd", "Mill Lane", "Harbor Ave", "Quarry St", "Depot Way", "Cedar Blvd"])
        town = r.choice(["Port Alder, WA", "Fenwick, OR", "Bristow, ID", "Marlow, MT"])
        return f"{r.randint(10, 990)} {street}, {town} {r.randint(97000, 99400)}"

    def _next_inv(self, vid: int) -> str:
        self._inv_seq[vid] += self.rng.randint(1, 9)
        return self.vendors[vid][6].format(n=self._inv_seq[vid])

    def _date(self) -> date:
        return self.start + timedelta(days=self.rng.randint(0, 80))

    def _new_po(self, vid: int, short_receipt: bool = False) -> dict:
        r = self.rng
        prefix = self.vendors[vid][5]
        chosen = r.sample(list(enumerate(ITEMS[prefix], 1)), r.randint(1, len(ITEMS[prefix])))
        lines = []
        for idx, (desc, price) in sorted(chosen):
            qty = Decimal(r.choice([4, 6, 10, 12, 20, 24, 40, 50, 80, 100]))
            lines.append({"sku": f"{prefix}-{1000 + idx}", "description": desc, "qty_ordered": qty,
                          "unit_price": q(price), "qty_received": qty})
        if short_receipt:  # part of the order has not arrived
            ln = r.choice(lines)
            ln["qty_received"] = (ln["qty_ordered"] * Decimal("0.5")).to_integral_value()
        self._po_seq += r.randint(1, 7)
        po = {"po_number": f"PO-{self._po_seq}", "vendor_id": vid, "lines": lines}
        self.pos.append(po)
        return po

    def _doc(self, vid: int, lines: list[dict], scenario: str, **over) -> dict:
        _, _, printed, _, layout, _, _ = self.vendors[vid]
        d = self._date()
        subtotal = sum((ln["amount"] for ln in lines), Decimal("0"))
        tax = q(subtotal * Decimal(str(over.pop("tax_rate", 0))))
        doc = {
            "scenario": scenario, "layout": layout, "vendor_id": vid, "vendor_name": printed,
            "vendor_addr": self._addr(), "invoice_number": self._next_inv(vid), "invoice_date": d,
            "due_date": d + timedelta(days=30), "terms_days": 30, "po_number": None,
            "lines": lines, "subtotal": subtotal, "tax": tax, "total": subtotal + tax,
        }
        doc.update(over)
        return doc

    @staticmethod
    def _bill_lines(po: dict) -> list[dict]:
        """Invoice lines that bill exactly what was received at the PO price."""
        return [{"sku": ln["sku"], "description": ln["description"], "qty": ln["qty_received"],
                 "unit_price": ln["unit_price"], "amount": q(ln["qty_received"] * ln["unit_price"])}
                for ln in po["lines"]]

    def _inventory_vendor(self, freeform: bool = False) -> int:
        pool = [v[0] for v in VENDORS if v[3] == "inventory" and (v[4] == "D") == freeform]
        return self.rng.choice(pool)

    # ---- scenarios ----
    def clean(self, scenario: str = "clean", freeform: bool = False) -> dict:
        vid = self._inventory_vendor(freeform)
        po = self._new_po(vid)
        return self._doc(vid, self._bill_lines(po), scenario, po_number=po["po_number"])

    def price(self, scenario: str) -> dict:
        vid = self._inventory_vendor()
        po = self._new_po(vid)
        lines = self._bill_lines(po)
        ln = self.rng.choice(lines)
        bump = self.rng.uniform(1.010, 1.015) if scenario == "price_within_tolerance" else self.rng.uniform(1.04, 1.12)
        ln["unit_price"] = q(ln["unit_price"] * Decimal(str(bump)))
        ln["amount"] = q(ln["qty"] * ln["unit_price"])
        return self._doc(vid, lines, scenario, po_number=po["po_number"])

    def qty_over(self) -> dict:
        vid = self._inventory_vendor()
        if self.rng.random() < 0.5:  # billed for the full order, half of one line never arrived
            po = self._new_po(vid, short_receipt=True)
            lines = self._bill_lines(po)
            for ln, pl in zip(lines, po["lines"]):
                ln["qty"] = pl["qty_ordered"]
                ln["amount"] = q(ln["qty"] * ln["unit_price"])
        else:                        # billed for more than was ordered or received
            po = self._new_po(vid)
            lines = self._bill_lines(po)
            ln = self.rng.choice(lines)
            ln["qty"] += Decimal(self.rng.choice([1, 2, 5, 10]))
            ln["amount"] = q(ln["qty"] * ln["unit_price"])
        return self._doc(vid, lines, "qty_over_receipt", po_number=po["po_number"])

    def missing_po(self) -> dict:
        vid = self._inventory_vendor()
        po = self._new_po(vid)
        return self._doc(vid, self._bill_lines(po), "missing_po")

    def math_error(self) -> dict:
        doc = self.clean("math_error")
        doc["total"] += q(self.rng.choice([10, 25, 100, 250]))
        return doc

    def unknown_vendor(self) -> dict:
        name, layout, prefix = self.rng.choice(UNKNOWN_VENDORS)
        lines = []
        for idx, (desc, price) in enumerate(ITEMS[prefix], 1):
            qty = Decimal(self.rng.choice([1, 2, 4, 10]))
            lines.append({"sku": f"{prefix}-{1000 + idx}", "description": desc, "qty": qty,
                          "unit_price": q(price), "amount": q(qty * q(price))})
        d = self._date()
        subtotal = sum((ln["amount"] for ln in lines), Decimal("0"))
        return {"scenario": "unknown_vendor", "layout": layout, "vendor_id": None, "vendor_name": name,
                "vendor_addr": self._addr(), "invoice_number": str(self.rng.randint(100, 999)),
                "invoice_date": d, "due_date": d + timedelta(days=15), "terms_days": 15,
                "po_number": f"PO-{self.rng.randint(90000, 99999)}", "lines": lines,
                "subtotal": subtotal, "tax": Decimal("0.00"), "total": subtotal}

    def expense(self, spike: bool) -> dict:
        vid = self.rng.choice(list(EXPENSE))
        desc, typical, tax_rate = EXPENSE[vid]
        factor = self.rng.uniform(1.45, 1.9) if spike else self.rng.uniform(0.97, 1.03)
        amount = q(typical * factor)
        prefix = self.vendors[vid][5]
        line = {"sku": f"{prefix}-100", "description": desc, "qty": Decimal(1),
                "unit_price": amount, "amount": amount}
        return self._doc(vid, [line], "expense_spike" if spike else "expense_normal", tax_rate=tax_rate)

    def statement(self) -> dict:
        vid = self._inventory_vendor()
        d = self._date()
        items = [(self._next_inv(vid), d - timedelta(days=self.rng.randint(5, 50)),
                  q(self.rng.uniform(200, 4000))) for _ in range(self.rng.randint(2, 5))]
        return {"scenario": "statement", "layout": "statement", "vendor_id": vid,
                "vendor_name": self.vendors[vid][2], "vendor_addr": self._addr(), "invoice_number": None,
                "invoice_date": d, "due_date": None, "po_number": None, "lines": [], "open_items": items,
                "subtotal": None, "tax": None, "total": sum((i[2] for i in items), Decimal("0"))}

    def bill_history(self) -> None:
        for vid, (_, typical, tax_rate) in EXPENSE.items():
            for m in range(1, 13):
                amount = q(typical * (1 + tax_rate) * self.rng.uniform(0.97, 1.03))
                self.history.append({"vendor_id": vid, "amount": amount,
                                     "bill_date": self.start - timedelta(days=30 * m)})


def _jsonable(o):
    if isinstance(o, Decimal):
        return str(o)
    if isinstance(o, date):
        return o.isoformat()
    raise TypeError(type(o))


def generate(out_dir: Path, n: int = 120, seed: int = 7) -> dict:
    """Write PDFs, master_data.json and ground_truth.json under out_dir."""
    w = _World(seed)
    w.bill_history()
    counts = {s: max(1, round(n * share)) for s, share in MIX.items()}
    n_copies = counts.pop("duplicate"), counts.pop("resubmitted_new_number")
    builders = {
        "statement": w.statement, "expense_normal": lambda: w.expense(False),
        "expense_spike": lambda: w.expense(True), "freeform": lambda: w.clean("freeform", freeform=True),
        "price_within_tolerance": lambda: w.price("price_within_tolerance"),
        "price_variance": lambda: w.price("price_variance"), "qty_over_receipt": w.qty_over,
        "unknown_vendor": w.unknown_vendor, "missing_po": w.missing_po, "math_error": w.math_error,
    }
    docs = [builders[s]() for s, k in counts.items() for _ in range(k)]
    n_clean = max(sum(n_copies), n - len(docs) - sum(n_copies))
    docs += [w.clean() for _ in range(n_clean)]
    w.rng.shuffle(docs)

    # Copies arrive after their originals, the way a vendor's second send does.
    originals = w.rng.sample([d for d in docs if d["scenario"] == "clean"], sum(n_copies))
    for i, original in enumerate(originals):
        copy = dict(original)
        if i < n_copies[0]:
            copy["scenario"] = "duplicate"           # byte-for-byte the same request
        else:
            copy["scenario"] = "resubmitted_new_number"  # same goods, new number: slips past a number check
            copy["invoice_number"] = original["invoice_number"] + "-A"
            copy["invoice_date"] = original["invoice_date"] + timedelta(days=9)
            copy["due_date"] = original["due_date"] + timedelta(days=9)
        position = w.rng.randint(docs.index(original) + 1, len(docs))
        docs.insert(position, copy)

    pdf_dir = out_dir / "invoices"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    for old in pdf_dir.glob("*.pdf"):
        old.unlink()
    truth = []
    for i, doc in enumerate(docs, 1):
        name = f"doc_{i:04d}.pdf"
        layouts.render(doc, pdf_dir / name)
        decision, exception = EXPECTED[doc["scenario"]]
        truth.append({
            "file": name, "scenario": doc["scenario"], "layout": doc["layout"],
            "expected_decision": decision, "expected_exception": exception,
            "is_invoice": doc["layout"] != "statement",
            "fields": {k: doc[k] for k in ("vendor_name", "vendor_id", "invoice_number", "invoice_date",
                                           "due_date", "po_number", "subtotal", "tax", "total")},
            "lines": [{"line_no": j, **ln} for j, ln in enumerate(doc["lines"], 1)],
        })

    master = {
        "vendors": [{"id": v[0], "name": v[1], "category": v[3]} for v in VENDORS],
        "purchase_orders": w.pos,
        "bill_history": w.history,
    }
    (out_dir / "master_data.json").write_text(json.dumps(master, indent=1, default=_jsonable))
    (out_dir / "ground_truth.json").write_text(json.dumps(truth, indent=1, default=_jsonable))
    return {"documents": len(docs), "purchase_orders": len(w.pos), "out_dir": str(out_dir)}
