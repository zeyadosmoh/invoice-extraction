"""Provider-agnostic extractor interface."""

from __future__ import annotations

import hashlib
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from .schema import ExtractedInvoice

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "extract_invoice.md"

MIME_TYPES = {".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}


@dataclass
class Prompt:
    text: str
    version: str
    sha256: str


def load_prompt(path: Path = PROMPT_PATH) -> Prompt:
    """Load the versioned prompt. The first line must be `<!-- prompt-version: X -->`; it is stripped."""
    raw = path.read_text(encoding="utf-8")
    m = re.match(r"\s*<!--\s*prompt-version:\s*(\S+)\s*-->\s*\n", raw)
    if not m:
        raise ValueError(f"{path} must start with '<!-- prompt-version: X -->'")
    text = raw[m.end():].strip()
    return Prompt(text=text, version=m.group(1), sha256=hashlib.sha256(text.encode()).hexdigest()[:12])


@dataclass
class ExtractionResult:
    doc_id: str
    source_file: str
    invoice: ExtractedInvoice
    raw_response: str
    provider: str
    model: str
    prompt_version: str
    prompt_sha256: str
    input_tokens: int | None
    output_tokens: int | None  # billed output, incl. thinking tokens where the provider reports them
    latency_s: float
    extra: dict = field(default_factory=dict)

    def to_json_dict(self) -> dict:
        return {
            "doc_id": self.doc_id,
            "source_file": self.source_file,
            "provider": self.provider,
            "model": self.model,
            "prompt_version": self.prompt_version,
            "prompt_sha256": self.prompt_sha256,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_s": round(self.latency_s, 3),
            "extra": self.extra,
            "invoice": self.invoice.model_dump(mode="json"),
            "raw_response": self.raw_response,
        }


class ExtractionError(Exception):
    """Base error. Messages must never contain credentials; build them with `redact`."""


class ConfigError(ExtractionError):
    pass


class ResponseParseError(ExtractionError):
    """The model answered but the payload did not validate. Keeps the raw text for debugging."""

    def __init__(self, message: str, raw_response: str):
        super().__init__(message)
        self.raw_response = raw_response


class RateLimitError(ExtractionError):
    """Transient (per-minute) rate limit; worth retrying after a backoff."""

    def __init__(self, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.retry_after_s = retry_after_s


class QuotaExhaustedError(ExtractionError):
    """Daily / hard quota reached; retrying today is pointless."""


def redact(text: str, *secrets: str | None) -> str:
    """Remove secrets and anything that looks like a Google API key from a message."""
    for s in secrets:
        if s:
            text = text.replace(s, "[REDACTED]")
    return re.sub(r"AIza[0-9A-Za-z_\-]{20,}", "[REDACTED]", text)


class Extractor(ABC):
    provider: str
    model: str

    @abstractmethod
    def extract(self, path: Path) -> ExtractionResult:
        """Extract one invoice document (PDF or image)."""


def mime_type_for(path: Path) -> str:
    try:
        return MIME_TYPES[path.suffix.lower()]
    except KeyError:
        raise ExtractionError(f"unsupported file type: {path.suffix}") from None
