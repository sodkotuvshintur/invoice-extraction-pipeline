"""Engine selection: templates first, the LLM only for what templates cannot read."""
from __future__ import annotations

from typing import Optional, Protocol

from ..models import Extraction
from . import rules


class Engine(Protocol):
    def extract(self, pdf_bytes: bytes) -> Extraction: ...


class CompositeExtractor:
    def __init__(self, llm: Optional[Engine] = None):
        self.llm = llm

    def extract(self, pdf_bytes: bytes) -> Extraction:
        result = rules.extract(pdf_bytes)
        if result is not None:
            return result
        if self.llm is None:
            return Extraction(engine="none", notes=["no template matched and no LLM engine configured"])
        return self.llm.extract(pdf_bytes)
