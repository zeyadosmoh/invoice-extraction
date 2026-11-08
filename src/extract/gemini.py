"""Gemini extractor using the official `google-genai` SDK (client.models.generate_content).

PDFs are sent as application/pdf bytes and PNGs as image bytes. The model returns JSON
constrained by RESPONSE_JSON_SCHEMA. The SDK's own retries are disabled so that
run_extraction.py owns throttling and backoff.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from google import genai
from google.genai import errors, types

from .base import (
    ConfigError,
    ExtractionError,
    ExtractionResult,
    Extractor,
    QuotaExhaustedError,
    RateLimitError,
    ResponseParseError,
    load_prompt,
    mime_type_for,
    redact,
)
from .schema import RESPONSE_JSON_SCHEMA, parse_invoice

DEFAULT_MODEL = "gemini-3.5-flash-lite"
_PLACEHOLDERS = {"", "your_api_key_here"}


def _api_key() -> str:
    for name in ("GEMINI_API_KEY", "LLM_API_KEY"):
        v = os.environ.get(name, "").strip()
        if v not in _PLACEHOLDERS:
            return v
    raise ConfigError("No Gemini API key found: set GEMINI_API_KEY in .env (see .env.example).")


def _retry_delay_s(details: object) -> float | None:
    """Read google.rpc.RetryInfo.retryDelay (e.g. '29s') from an error payload."""
    for d in _error_details(details):
        if str(d.get("@type", "")).endswith("RetryInfo"):
            m = re.match(r"([\d.]+)s", str(d.get("retryDelay", "")))
            if m:
                return float(m.group(1))
    return None


def _is_daily_quota(details: object) -> bool:
    """True when a QuotaFailure violation names a per-day quota."""
    for d in _error_details(details):
        if str(d.get("@type", "")).endswith("QuotaFailure"):
            for v in d.get("violations", []) or []:
                if "perday" in str(v.get("quotaId", "")).lower().replace("_", ""):
                    return True
    return False


def _error_details(details: object) -> list[dict]:
    if isinstance(details, dict):
        inner = details.get("error", details).get("details", [])
        return [d for d in inner if isinstance(d, dict)]
    return []


class GeminiExtractor(Extractor):
    provider = "gemini"

    def __init__(self, model: str | None = None, timeout_s: float = 180.0):
        self.model = model or os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL
        self._key = _api_key()
        self._client = genai.Client(
            api_key=self._key,
            http_options=types.HttpOptions(
                timeout=int(timeout_s * 1000),
                retry_options=types.HttpRetryOptions(attempts=1),
            ),
        )
        self._prompt = load_prompt()
        temp = os.environ.get("LLM_TEMPERATURE", "").strip()
        # Unset by default: Google recommends the default temperature for Gemini 3-series models.
        self._temperature = float(temp) if temp else None

    def _safe(self, text: str) -> str:
        return redact(text, self._key)

    def extract(self, path: Path) -> ExtractionResult:
        path = Path(path)
        part = types.Part.from_bytes(data=path.read_bytes(), mime_type=mime_type_for(path))
        config = types.GenerateContentConfig(
            response_mime_type="application/json",
            response_json_schema=RESPONSE_JSON_SCHEMA,
            temperature=self._temperature,
        )
        t0 = time.perf_counter()
        try:
            resp = self._client.models.generate_content(
                model=self.model, contents=[part, self._prompt.text], config=config
            )
        except errors.APIError as e:
            msg = self._safe(f"{e.code} {e.status}: {e.message}")
            if e.code == 429:
                if _is_daily_quota(e.details):
                    raise QuotaExhaustedError(msg) from None
                raise RateLimitError(msg, _retry_delay_s(e.details)) from None
            raise ExtractionError(msg) from None
        except Exception as e:  # network errors etc.; never chain (tracebacks could carry request data)
            raise ExtractionError(self._safe(f"{type(e).__name__}: {e}")) from None
        latency = time.perf_counter() - t0

        raw = resp.text or ""
        usage = resp.usage_metadata
        candidates = (usage.candidates_token_count or 0) if usage else 0
        thoughts = (usage.thoughts_token_count or 0) if usage else 0
        finish = resp.candidates[0].finish_reason if resp.candidates else None
        try:
            invoice = parse_invoice(raw)
        except Exception as e:
            raise ResponseParseError(
                self._safe(f"could not parse model response (finish_reason={finish}): {e}"), raw
            ) from None

        return ExtractionResult(
            doc_id=path.stem,
            source_file=path.name,
            invoice=invoice,
            raw_response=raw,
            provider=self.provider,
            model=self.model,
            prompt_version=self._prompt.version,
            prompt_sha256=self._prompt.sha256,
            input_tokens=usage.prompt_token_count if usage else None,
            output_tokens=(candidates + thoughts) if usage else None,
            latency_s=latency,
            extra={
                "candidates_tokens": candidates,
                "thoughts_tokens": thoughts,
                "finish_reason": str(finish) if finish else None,
                "model_version": getattr(resp, "model_version", None),
            },
        )
