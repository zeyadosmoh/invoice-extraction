"""Offline tests for the extraction module: no network, no API key needed."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from extract import get_extractor  # noqa: E402
from extract.base import (  # noqa: E402
    ExtractionResult,
    Extractor,
    ExtractionError,
    QuotaExhaustedError,
    RateLimitError,
    ResponseParseError,
    TransientError,
    load_prompt,
    redact,
)
from extract.gemini import _is_daily_quota, _retry_delay_s  # noqa: E402
from extract.normalize import normalize_date, normalize_digits, normalize_number  # noqa: E402
from extract.schema import ExtractedInvoice, parse_invoice  # noqa: E402
from compare_samples import compare_value  # noqa: E402
from run_extraction import RUNS_COLUMNS, run  # noqa: E402

SAMPLES = ROOT / "data" / "samples"

# A response shaped like Gemini's structured output for doc_0045 (Arabic-Indic digits left in on purpose).
SAMPLE_RESPONSE = json.dumps({
    "vendor_name": "Wahat Digital Solutions",
    "vendor_name_ar": "حلول الواحة الرقمية",
    "vendor_tax_id": "٣٠٢١١٧٧٣٥٤٠٠٠٠٣",
    "buyer_name": "Orchard Peak Arabia Co.",
    "invoice_number": "WDS/2025/0506",
    "invoice_date": "١٨.٠٨.٢٠٢٥",
    "due_date": "2025-08-18",
    "currency": "sar",
    "line_items": [
        {"description": "Installation and setup (h)", "quantity": "٣٫٥", "unit_price": "١٥٤٫٠٠", "line_total": "539.00"},
        {"description": "Wireless keyboard and mouse", "quantity": "1", "unit_price": "252.41", "line_total": "252.41"},
    ],
    "subtotal": "١٬٠١٩٫٤١",
    "discount": "50.97",
    "tax_rate": "15%",
    "tax_amount": "145.27",
    "total": "SAR 1,113.71",
    "uncertain_fields": ["line_items[1].unit_price"],
}, ensure_ascii=False)


# ---------------------------------------------------------------- normalisation

@pytest.mark.parametrize("raw, expected", [
    ("٠١٢٣٤٥٦٧٨٩", "0123456789"),
    ("۰۱۲۳۴۵۶۷۸۹", "0123456789"),
    ("١٢٣٫٤٥", "123.45"),
    ("١٬٢٣٤٫٥٠", "1234.50"),
    ("WDS/٢٠٢٥/٠٥٠٦", "WDS/2025/0506"),
    ("‏١٥‏", "15"),
    ("plain text", "plain text"),
])
def test_normalize_digits(raw, expected):
    assert normalize_digits(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("1,234.56", "1234.56"),
    ("1.234,56", "1234.56"),
    ("1 234,56", "1234.56"),
    ("١٬٢٣٤٫٥٦", "1234.56"),
    ("12,5", "12.5"),
    ("1,234", "1234"),
    ("1.234.567", "1234567"),
    ("3.5", "3.5"),
    ("154.00", "154.00"),
    ("15%", "15"),
    ("$ 99.90", "99.90"),
    ("-50.97", "-50.97"),
    ("(50.97)", "-50.97"),
    (12, "12"),
    (None, None),
    ("", None),
    ("n/a", None),
])
def test_normalize_number(raw, expected):
    assert normalize_number(raw) == expected


@pytest.mark.parametrize("raw, expected", [
    ("2025-03-14", "2025-03-14"),
    ("14.03.2025", "2025-03-14"),
    ("March 14, 2025", "2025-03-14"),
    ("14 Mar 2025", "2025-03-14"),
    ("١٤.٠٣.٢٠٢٥", "2025-03-14"),
    ("03/04/2025", "03/04/2025"),  # ambiguous: left alone so validation rejects it
    (None, None),
])
def test_normalize_date(raw, expected):
    assert normalize_date(raw) == expected


# ---------------------------------------------------------------- schema parsing

def test_parse_sample_response():
    inv = parse_invoice(SAMPLE_RESPONSE)
    assert inv.vendor_tax_id == "302117735400003"
    assert inv.invoice_date == date(2025, 8, 18)
    assert inv.currency == "SAR"
    assert inv.subtotal == Decimal("1019.41")
    assert inv.tax_rate == Decimal("15")
    assert inv.total == Decimal("1113.71")
    assert inv.line_items[0].quantity == Decimal("3.5")
    assert inv.line_items[0].unit_price == Decimal("154.00")
    assert inv.uncertain_fields == ["line_items[1].unit_price"]
    # Decimals serialise as exact strings, dates as ISO.
    dumped = inv.model_dump(mode="json")
    assert dumped["subtotal"] == "1019.41" and dumped["invoice_date"] == "2025-08-18"


def test_parse_keeps_nulls():
    inv = parse_invoice({"invoice_date": None, "discount": None, "vendor_name_ar": "  ", "line_items": []})
    assert inv.invoice_date is None and inv.discount is None and inv.vendor_name_ar is None


def test_parse_rejects_ambiguous_date_and_unknown_fields():
    with pytest.raises(ValueError):
        parse_invoice({"invoice_date": "03/04/2025"})
    with pytest.raises(ValueError):
        parse_invoice({"vendor_name": "X", "made_up_field": 1})


def test_samples_ground_truth_fits_schema():
    """Every ground-truth invoice in data/samples must be representable by the schema."""
    for p in sorted(SAMPLES.glob("doc_*.json")):
        gt = json.loads(p.read_text(encoding="utf-8"))["invoice"]
        gt = {k: v for k, v in gt.items()}
        gt["line_items"] = [{k: v for k, v in li.items() if k != "description_ar"} for li in gt["line_items"]]
        ExtractedInvoice.model_validate(gt)


def test_prompt_is_versioned_and_states_rules():
    pr = load_prompt()
    assert pr.version and len(pr.sha256) == 12
    for phrase in ("exactly as it is printed", "Arabic-Indic", "٫", "YYYY-MM-DD", "null"):
        assert phrase in pr.text


# ---------------------------------------------------------------- secrets / errors

def test_redact():
    key = "AIza" + "x" * 35
    assert key not in redact(f"bad key {key}", key)
    fake = "AIza" + "Sy" + "A" * 33  # built at runtime so secret scanners don't flag a literal
    assert "AIza" not in redact(f"leaked {fake}")


def test_quota_detection_from_error_details():
    daily = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED", "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier"}]},
        {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "41s"},
    ]}}
    minute = {"error": {"details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure",
         "violations": [{"quotaId": "GenerateRequestsPerMinutePerProjectPerModel-FreeTier"}]},
    ]}}
    assert _is_daily_quota(daily) and not _is_daily_quota(minute)
    assert _retry_delay_s(daily) == 41.0 and _retry_delay_s(minute) is None


def test_provider_selection(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "claude")
    ex = get_extractor()
    with pytest.raises(NotImplementedError):
        ex.extract(Path("x.pdf"))
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(NotImplementedError):
        get_extractor().extract(Path("x.pdf"))


def test_gemini_requires_key(monkeypatch):
    from extract.base import ConfigError

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("LLM_API_KEY", "your_api_key_here")
    with pytest.raises(ConfigError):
        get_extractor()


# ---------------------------------------------------------------- runner with a fake extractor

class FakeExtractor(Extractor):
    provider = "fake"
    model = "fake-model/v1"

    def __init__(self, script: dict[str, list] | None = None):
        self.script = script or {}  # doc_id -> list of exceptions to raise before succeeding
        self.calls: list[str] = []

    def extract(self, path: Path) -> ExtractionResult:
        self.calls.append(path.stem)
        queue = self.script.get(path.stem, [])
        if queue:
            raise queue.pop(0)
        return ExtractionResult(
            doc_id=path.stem, source_file=path.name, invoice=parse_invoice(SAMPLE_RESPONSE),
            raw_response=SAMPLE_RESPONSE, provider=self.provider, model=self.model,
            prompt_version="test", prompt_sha256="0" * 12, input_tokens=100, output_tokens=50, latency_s=0.5,
        )


@pytest.fixture
def docs(tmp_path):
    d = tmp_path / "in"
    d.mkdir()
    for name in ("doc_a.pdf", "doc_b.png", "doc_c.pdf", "notes.txt"):
        (d / name).write_bytes(b"x")
    return d


def _rows(out: Path) -> list[dict]:
    with (out / "fake-model_v1" / "runs.csv").open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_runner_caches_and_logs(docs, tmp_path):
    out, sleeps = tmp_path / "out", []
    ex = FakeExtractor()
    s = run(ex, docs, out, delay_s=7, sleep=sleeps.append, log=lambda m: None)
    assert s.ok == ["doc_a.pdf", "doc_b.png", "doc_c.pdf"] and s.exit_code == 0
    assert sleeps == [7, 7]  # throttle between requests, not before the first
    saved = json.loads((out / "fake-model_v1" / "doc_a.pdf.json").read_text(encoding="utf-8"))
    assert saved["invoice"]["total"] == "1113.71" and saved["input_tokens"] == 100
    assert len(saved["source_sha256"]) == 64
    rows = _rows(out)
    assert [r["file_type"] for r in rows] == ["pdf", "png", "pdf"]
    assert all(r["status"] == "ok" for r in rows)

    # Second run: everything cached, no calls.
    ex2 = FakeExtractor()
    s2 = run(ex2, docs, out, sleep=lambda _: None, log=lambda m: None)
    assert ex2.calls == [] and s2.cached == ["doc_a.pdf", "doc_b.png", "doc_c.pdf"]

    # --force re-runs.
    ex3 = FakeExtractor()
    run(ex3, docs, out, force=True, sleep=lambda _: None, log=lambda m: None)
    assert ex3.calls == ["doc_a", "doc_b", "doc_c"]


def test_runner_retries_rate_limit_with_backoff(docs, tmp_path):
    sleeps = []
    ex = FakeExtractor({"doc_a": [RateLimitError("429"), RateLimitError("429", retry_after_s=30)]})
    s = run(ex, docs, tmp_path / "out", delay_s=1, backoff_base_s=2, backoff_cap_s=60,
            sleep=sleeps.append, log=lambda m: None)
    assert s.ok == ["doc_a.pdf", "doc_b.png", "doc_c.pdf"]
    assert ex.calls[:3] == ["doc_a", "doc_a", "doc_a"]
    backoffs = [x for x in sleeps if x != 1]
    assert 1 <= backoffs[0] <= 2 and backoffs[1] >= 30  # jittered exponential, honours retry hint
    assert _rows(tmp_path / "out")[0]["attempts"] == "3"


def test_runner_stops_when_rate_limit_persists(docs, tmp_path):
    ex = FakeExtractor({"doc_b": [RateLimitError("429")] * 10})
    s = run(ex, docs, tmp_path / "out", max_retries=2, sleep=lambda _: None, log=lambda m: None)
    assert s.ok == ["doc_a.pdf"] and s.exit_code == 2 and "daily quota" in s.stopped_reason
    assert "doc_c" not in ex.calls


def test_runner_stops_on_daily_quota(docs, tmp_path):
    ex = FakeExtractor({"doc_b": [QuotaExhaustedError("429 RESOURCE_EXHAUSTED")]})
    s = run(ex, docs, tmp_path / "out", sleep=lambda _: None, log=lambda m: None)
    assert s.ok == ["doc_a.pdf"] and s.exit_code == 2
    assert ex.calls == ["doc_a", "doc_b"]
    assert _rows(tmp_path / "out")[-1]["status"] == "quota_exhausted"


def test_runner_continues_after_parse_error(docs, tmp_path):
    out = tmp_path / "out"
    ex = FakeExtractor({"doc_a": [ResponseParseError("bad json", raw_response="{oops")]})
    s = run(ex, docs, out, sleep=lambda _: None, log=lambda m: None)
    assert s.failed == ["doc_a.pdf"] and s.ok == ["doc_b.png", "doc_c.pdf"] and s.exit_code == 1
    assert (out / "fake-model_v1" / "doc_a.pdf.raw.txt").read_text() == "{oops"
    assert not (out / "fake-model_v1" / "doc_a.pdf.json").exists()


def _quiet(**kw):
    return dict(sleep=lambda _: None, log=lambda m: None, **kw)


def test_pdf_and_scan_of_same_doc_are_cached_separately(tmp_path):
    """doc_0001.pdf and doc_0001.png must never overwrite or skip each other, same folder or not."""
    out = tmp_path / "out"
    pdfs, scans = tmp_path / "pdf", tmp_path / "scans"
    pdfs.mkdir(); scans.mkdir()
    (pdfs / "doc_0001.pdf").write_bytes(b"pdf bytes")
    (scans / "doc_0001.png").write_bytes(b"png bytes")

    ex = FakeExtractor()
    assert run(ex, pdfs, out, **_quiet()).ok == ["doc_0001.pdf"]
    s = run(ex, scans, out, **_quiet())
    assert s.ok == ["doc_0001.png"] and s.cached == []  # the PDF result did not cause a skip
    model_dir = out / "fake-model_v1"
    a = json.loads((model_dir / "doc_0001.pdf.json").read_text(encoding="utf-8"))
    b = json.loads((model_dir / "doc_0001.png.json").read_text(encoding="utf-8"))
    assert a["source_file"] == "doc_0001.pdf" and b["source_file"] == "doc_0001.png"

    # Both in one folder: two keys, no duplicate-id error; both now cached.
    both = tmp_path / "both"
    both.mkdir()
    (both / "doc_0001.pdf").write_bytes(b"pdf bytes")
    (both / "doc_0001.png").write_bytes(b"png bytes")
    assert run(FakeExtractor(), both, out, **_quiet()).cached == ["doc_0001.pdf", "doc_0001.png"]

    rows = list(csv.DictReader((model_dir / "runs.csv").open(encoding="utf-8")))
    assert [r["doc_key"] for r in rows[:2]] == ["doc_0001.pdf", "doc_0001.png"]


def test_cache_is_invalidated_when_source_file_changes(tmp_path):
    d, out = tmp_path / "in", tmp_path / "out"
    d.mkdir()
    (d / "doc_x.pdf").write_bytes(b"v1")
    run(FakeExtractor(), d, out, **_quiet())
    (d / "doc_x.pdf").write_bytes(b"v2")
    ex = FakeExtractor()
    assert run(ex, d, out, **_quiet()).ok == ["doc_x.pdf"] and ex.calls == ["doc_x"]


def test_legacy_cache_and_runs_csv_are_migrated(tmp_path):
    d, out = tmp_path / "in", tmp_path / "out"
    model_dir = out / "fake-model_v1"
    d.mkdir(); model_dir.mkdir(parents=True)
    (d / "doc_0001.png").write_bytes(b"scan")
    (d / "doc_0002.pdf").write_bytes(b"pdf")
    # Pre-v2 layout: <doc_id>.json, no hash; old runs.csv header without doc_key / input_dir.
    (model_dir / "doc_0001.json").write_text(json.dumps({"source_file": "doc_0001.png", "invoice": {}}))
    (model_dir / "doc_0002.json").write_text(json.dumps({"source_file": "doc_0002.png", "invoice": {}}))
    (model_dir / "runs.csv").write_text("run_at,doc_id,file_type,status\n2026-01-01,doc_0001,png,ok\n")

    ex = FakeExtractor()
    s = run(ex, d, out, **_quiet())
    assert s.cached == ["doc_0001.png"]  # legacy result reused for the matching file type
    assert s.ok == ["doc_0002.pdf"]      # legacy result from a *png* must not count for the pdf
    assert (model_dir / "doc_0001.png.json").exists() and not (model_dir / "doc_0001.json").exists()
    assert (model_dir / "doc_0002.json").exists()  # left alone: it belongs to a png not in this folder

    rows = list(csv.DictReader((model_dir / "runs.csv").open(encoding="utf-8")))
    assert list(rows[0].keys()) == RUNS_COLUMNS
    assert rows[0]["doc_key"] == "doc_0001.png" and rows[0]["status"] == "ok"


def test_transient_errors_retry_then_skip_doc(docs, tmp_path):
    ex = FakeExtractor({"doc_a": [TransientError("503")] * 10})
    s = run(ex, docs, tmp_path / "out", max_retries=2, **_quiet())
    assert s.failed == ["doc_a.pdf"] and s.ok == ["doc_b.png", "doc_c.pdf"] and s.stopped_reason is None
    assert ex.calls.count("doc_a") == 3


def test_stops_after_consecutive_errors(docs, tmp_path):
    ex = FakeExtractor({k: [ExtractionError("400 INVALID_ARGUMENT")] for k in ("doc_a", "doc_b", "doc_c")})
    s = run(ex, docs, tmp_path / "out", max_consecutive_errors=2, **_quiet())
    assert s.failed == ["doc_a.pdf", "doc_b.png"] and s.exit_code == 2 and "in a row" in s.stopped_reason
    assert "doc_c" not in ex.calls


# ---------------------------------------------------------------- comparison rules

@pytest.mark.parametrize("field, exp, got, status", [
    ("total", "56.70", "56.7", "match"),
    ("total", "56.70", "56.71", "diff"),
    ("invoice_date", None, None, "match"),
    ("invoice_date", None, "2025-07-18", "diff"),
    ("vendor_name", "Atelier Solenne SARL", " Atelier Solenne SARL ", "match"),
    ("vendor_name", "Atelier Solenne SARL", "ATELIER  SOLENNE sarl", "near"),
    ("vendor_name", "Atelier Solenne SARL", "Atelier Solene SARL", "diff"),
])
def test_compare_value(field, exp, got, status):
    assert compare_value(field, exp, got) == status
