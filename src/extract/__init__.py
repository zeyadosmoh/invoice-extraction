"""LLM invoice extraction. Pick a provider with LLM_PROVIDER (gemini | claude | openai)."""

from __future__ import annotations

import os

from .base import ConfigError, Extractor


def get_extractor(provider: str | None = None, model: str | None = None) -> Extractor:
    provider = (provider or os.environ.get("LLM_PROVIDER", "") or "gemini").strip().lower()
    if provider == "gemini":
        from .gemini import GeminiExtractor  # imported lazily so tests don't need the SDK configured

        return GeminiExtractor(model=model)
    if provider == "claude":
        from .claude import ClaudeExtractor

        return ClaudeExtractor(model=model)
    if provider == "openai":
        from .openai_extractor import OpenAIExtractor

        return OpenAIExtractor(model=model)
    raise ConfigError(f"unknown LLM_PROVIDER {provider!r}; expected gemini, claude or openai")
