"""Match the name printed on an invoice to a vendor in the master file.

The printed name is rarely the master name: legal suffixes, "&" vs "and",
capitals. Normalising removes those, and a normalised name that equals a
confirmed alias identifies the vendor (score 1.0).

Anything else is at most a suggestion. A close fuzzy match is returned with
its score so a reviewer sees "did you mean...", but the pipeline will not pay
on it: a name one letter off a real vendor is exactly what an impersonator
sends. Once a person confirms the name it is stored as an alias and matches
exactly from then on. Below the threshold there is no suggestion at all.
"""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Optional

_SUFFIXES = {"inc", "llc", "ltd", "co", "corp", "corporation", "company", "incorporated", "limited"}


def normalise(name: str) -> str:
    name = name.lower().replace("&", " and ")
    tokens = re.sub(r"[^a-z0-9 ]", " ", name).split()
    while tokens and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


@dataclass
class VendorMatch:
    vendor_id: int
    name: str
    category: str
    score: float


def resolve(conn: sqlite3.Connection, printed_name: Optional[str], min_score: float) -> Optional[VendorMatch]:
    if not printed_name:
        return None
    target = normalise(printed_name)
    best: Optional[VendorMatch] = None
    rows = conn.execute(
        "SELECT v.id, v.name, v.category, a.alias FROM vendor_alias a "
        "JOIN vendors v ON v.id = a.vendor_id WHERE v.status = 'active'")
    for row in rows:
        if row["alias"] == target:
            return VendorMatch(row["id"], row["name"], row["category"], 1.0)
        # compare without spaces so "blue water" and "bluewater" are the same word
        score = SequenceMatcher(None, target.replace(" ", ""), row["alias"].replace(" ", "")).ratio()
        if best is None or score > best.score:
            best = VendorMatch(row["id"], row["name"], row["category"], round(score, 3))
    return best if best and best.score >= min_score else None


def confirm_alias(conn: sqlite3.Connection, vendor_id: int, printed_name: str) -> None:
    """A person has confirmed this printed name belongs to this vendor."""
    conn.execute("INSERT OR IGNORE INTO vendor_alias (vendor_id, alias) VALUES (?, ?)",
                 (vendor_id, normalise(printed_name)))
