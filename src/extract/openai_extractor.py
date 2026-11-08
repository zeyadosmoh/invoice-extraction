"""Placeholder for an OpenAI extractor (not implemented yet).

Named openai_extractor.py so it can never shadow the `openai` package.
"""

from __future__ import annotations

from pathlib import Path

from .base import ExtractionResult, Extractor


class OpenAIExtractor(Extractor):
    provider = "openai"

    def __init__(self, model: str | None = None):
        self.model = model or "unset"

    def extract(self, path: Path) -> ExtractionResult:
        raise NotImplementedError("OpenAI extractor is not implemented yet.")
