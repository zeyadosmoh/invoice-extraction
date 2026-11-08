"""Placeholder for an Anthropic Claude extractor (not implemented yet)."""

from __future__ import annotations

from pathlib import Path

from .base import ExtractionResult, Extractor


class ClaudeExtractor(Extractor):
    provider = "claude"

    def __init__(self, model: str | None = None):
        self.model = model or "unset"

    def extract(self, path: Path) -> ExtractionResult:
        raise NotImplementedError("Claude extractor is not implemented yet.")
