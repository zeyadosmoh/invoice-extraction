"""Offline tests for the pipeline API (src/api.py): fake extractor, in-memory store, no network."""

from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from api import create_app  # noqa: E402
from extract.base import ConfigError, ExtractionResult, Extractor  # noqa: E402
from extract.schema import ExtractedInvoice, parse_invoice  # noqa: E402
from validate import Record  # noqa: E402

TOKEN = "test-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
TODAY = date(2026, 11, 8)


def invoice_json(**over) -> str:
    """A consistent invoice: 2 x 50.00 + 1 x 20.00 = 120.00, 5% tax = 6.00, total 126.00."""
    data = {
        "vendor_name": "Example Vendor LLC", "invoice_number": "EV-0100", "invoice_date": "2026-01-10",
        "due_date": "2026-02-09", "currency": "AED",
        "line_items": [
            {"description": "Item A", "quantity": "2", "unit_price": "50.00", "line_total": "100.00"},
            {"description": "Item B", "quantity": "1", "unit_price": "20.00", "line_total": "20.00"},
        ],
        "subtotal": "120.00", "discount": None, "tax_rate": "5", "tax_amount": "6.00", "total": "126.00",
    }
    data.update(over)
    return json.dumps(data)


class FakeExtractor(Extractor):
    provider = "fake"
    model = "fake-model"

    def __init__(self, responses: dict[str, str] | None = None):
        self.responses = responses or {}  # doc_id -> raw model response
        self.calls: list[str] = []

    def extract(self, path: Path) -> ExtractionResult:
        self.calls.append(path.name)
        raw = self.responses.get(path.stem, invoice_json())
        return ExtractionResult(
            doc_id=path.stem, source_file=path.name, invoice=parse_invoice(raw), raw_response=raw,
            provider=self.provider, model=self.model, prompt_version="test", prompt_sha256="0" * 12,
            input_tokens=10, output_tokens=5, latency_s=0.1,
        )


class FakeStore:
    """In-memory stand-in for Postgres: stored invoices are what the cross-document rules see."""

    def __init__(self, records: list[Record] | None = None):
        self.records = {r.doc_id: r for r in records or []}
        self.upserts: list[tuple] = []

    def existing_records(self) -> list[Record]:
        return [self.records[k] for k in sorted(self.records)]

    def upsert(self, invoice: tuple, lines: list, review: list) -> None:
        self.upserts.append((invoice, lines, review))
        doc_id, vendor, number, inv_date = invoice[0], invoice[3], invoice[7], invoice[8]
        self.records[doc_id] = Record(f"db:{doc_id}", doc_id, invoice[2], ExtractedInvoice(
            vendor_name=vendor, invoice_number=number, invoice_date=inv_date))

    def ping(self) -> None:
        pass


def stored(doc_id: str, number: str, inv_date: str, vendor: str = "Example Vendor LLC") -> Record:
    return Record(f"db:{doc_id}", doc_id, "pdf", ExtractedInvoice(
        vendor_name=vendor, invoice_number=number, invoice_date=date.fromisoformat(inv_date)))


@pytest.fixture
def env(tmp_path):
    def make(store=None, extractor=None, cache_only=False):
        store = store if store is not None else FakeStore()
        extractor = extractor or FakeExtractor()
        app = create_app(token=TOKEN, store=store, extractor_factory=lambda: extractor,
                         cache_root=tmp_path / "extracted", data_dir=tmp_path / "data",
                         cache_only=cache_only, today=lambda: TODAY)
        return TestClient(app), store, extractor
    return make


def upload(client, name="doc_0200.pdf", content=b"%PDF-1.4 fake", headers=AUTH):
    return client.post("/process", files={"file": (name, content, "application/pdf")}, headers=headers)


# ---------------------------------------------------------------- auth / health

def test_token_required(env):
    client, store, extractor = env()
    assert upload(client, headers={}).status_code == 401
    assert upload(client, headers={"Authorization": "Bearer wrong-token-0123456789"}).status_code == 401
    assert upload(client, headers={"Authorization": TOKEN}).status_code == 401  # no Bearer scheme
    assert client.post("/move", json={"filename": "doc_0200.pdf", "status": "auto_approved"}).status_code == 401
    assert extractor.calls == [] and store.upserts == []


def test_app_refuses_to_start_without_token(monkeypatch):
    monkeypatch.delenv("PIPELINE_API_TOKEN", raising=False)
    with pytest.raises(ConfigError):
        create_app(store=FakeStore())
    with pytest.raises(ConfigError):
        create_app(token="short", store=FakeStore())


def test_health_needs_no_token(env):
    client, _, _ = env()
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"ok": True, "db": "ok"}


# ---------------------------------------------------------------- /process

def test_clean_invoice_is_auto_approved_and_upserted(env):
    client, store, extractor = env()
    r = upload(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "auto_approved"
    assert body["failed_reasons"] == []
    assert body["filename"] == "doc_0200.pdf" and body["doc_id"] == "doc_0200"
    assert (body["vendor"], body["invoice_number"], body["total"], body["currency"]) == (
        "Example Vendor LLC", "EV-0100", "126.00", "AED")
    assert body["cached"] is False
    invoice, lines, review = store.upserts[0]
    assert invoice[0] == "doc_0200" and invoice[17] == "auto_approved" and len(lines) == 2 and review == []


def test_second_upload_of_same_file_uses_the_cache(env):
    client, store, extractor = env()
    upload(client)
    r = upload(client)
    assert r.json()["cached"] is True
    assert extractor.calls == ["doc_0200.pdf"]  # extracted once
    assert len(store.upserts) == 2  # but loaded (upserted) both times


def test_duplicate_is_checked_against_every_stored_invoice(env):
    store = FakeStore([stored("doc_0005", "EV-0100", "2026-01-10")])
    client, _, _ = env(store=store)
    body = upload(client).json()
    assert body["status"] == "needs_review"
    assert body["failed_reasons"] == [
        {"issue": "Duplicate invoice number", "detail": "Same invoice number as an invoice dated 2026-01-10"}]
    _, _, review = store.upserts[0]
    assert [r[1] for r in review] == ["duplicate"]


def test_resending_a_stored_invoice_is_not_a_duplicate_of_itself(env):
    store = FakeStore([stored("doc_0200", "EV-0100", "2026-01-10")])
    client, _, _ = env(store=store)
    assert upload(client).json()["status"] == "auto_approved"


def test_date_sequence_is_checked_against_stored_invoices(env):
    # Stored EV-0099 .. EV-0102 are in order; the new EV-0100 is dated months later (day/month swap).
    store = FakeStore([stored("doc_0001", "EV-0099", "2026-01-05"), stored("doc_0003", "EV-0101", "2026-01-20"),
                       stored("doc_0004", "EV-0102", "2026-01-25")])
    extractor = FakeExtractor({"doc_0200": invoice_json(invoice_date="2026-10-01", due_date="2026-10-30")})
    client, _, _ = env(store=store, extractor=extractor)
    body = upload(client).json()
    assert body["status"] == "needs_review"
    assert body["failed_reasons"] == [{"issue": "Invoice date out of sequence",
                                       "detail": "Invoice date is out of order with this vendor's other invoices"}]


@pytest.mark.parametrize("name", ["../doc_0200.pdf", "notes.txt", "doc 0200.pdf", ".hidden.pdf", "doc.pdf.exe"])
def test_bad_filenames_are_rejected(env, name):
    client, store, extractor = env()
    assert upload(client, name=name).status_code == 422
    assert extractor.calls == [] and store.upserts == []


def test_cache_only_mode_refuses_uncached_files(env):
    client, store, extractor = env(cache_only=True)
    r = upload(client)
    assert r.status_code == 409
    assert r.json()["detail"] == ("This file has not been read before, and reading new files is switched off "
                                  "(cache-only mode)")
    assert extractor.calls == [] and store.upserts == []


# ---------------------------------------------------------------- /move

def drop(tmp_path: Path, name: str) -> Path:
    inbox = tmp_path / "data" / "inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / name).write_bytes(b"%PDF fake")
    return inbox / name


def test_move_approved_file_to_processed(env, tmp_path):
    client, _, _ = env()
    drop(tmp_path, "doc_0200.pdf")
    r = client.post("/move", json={"filename": "doc_0200.pdf", "status": "auto_approved"}, headers=AUTH)
    assert r.status_code == 200 and r.json() == {
        "filename": "doc_0200.pdf", "moved_to": "data/processed/doc_0200.pdf", "logged": False}
    assert (tmp_path / "data" / "processed" / "doc_0200.pdf").is_file()
    assert not (tmp_path / "data" / "inbox" / "doc_0200.pdf").exists()
    assert not (tmp_path / "data" / "needs_review" / "review_log.csv").exists()


def test_move_flagged_file_appends_review_log(env, tmp_path):
    client, _, _ = env()
    reasons = [{"issue": "Tax amount wrong", "detail": "Tax should be 18.30 (15% of 122.00) but 21.96 is printed"},
               {"issue": "Duplicate invoice number", "detail": 'Same invoice "SHF-1" from حلول الواحة, dated 2025-02-07'}]
    for name in ("doc_0050.pdf", "doc_0051.png"):
        drop(tmp_path, name)
        body = {"filename": name, "doc_id": name.split(".")[0], "status": "needs_review",
                "failed_reasons": reasons, "vendor": "Wahat, Digital", "total": "140.30", "currency": "SAR"}
        r = client.post("/move", json=body, headers=AUTH)
        assert r.status_code == 200 and r.json()["logged"] is True
    nr = tmp_path / "data" / "needs_review"
    assert (nr / "doc_0050.pdf").is_file() and (nr / "doc_0051.png").is_file()
    with (nr / "review_log.csv").open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["filename"] for r in rows] == ["doc_0050.pdf", "doc_0051.png"]  # one header, two rows
    assert rows[0]["vendor"] == "Wahat, Digital"
    assert rows[0]["reasons"] == (
        "Tax amount wrong: Tax should be 18.30 (15% of 122.00) but 21.96 is printed; "
        'Duplicate invoice number: Same invoice "SHF-1" from حلول الواحة, dated 2025-02-07')


def test_reasons_never_show_ids_or_field_names(env):
    """Every failure the API can report is in plain English: no doc ids, no snake_case field names."""
    import re

    store = FakeStore([stored("doc_0005", "EV-0100", "2026-01-10")])
    bad = invoice_json(invoice_date=None, total="999.00", tax_amount="9.00", subtotal="130.00",
                       line_items=[{"description": "A", "quantity": "2", "unit_price": "50.00", "line_total": "90.00"}])
    client, _, _ = env(store=store, extractor=FakeExtractor({"doc_0200": bad}))
    body = upload(client).json()
    assert body["status"] == "needs_review" and len(body["failed_reasons"]) >= 4
    for r in body["failed_reasons"]:
        assert set(r) == {"issue", "detail"}
        text = r["issue"] + " " + r["detail"]
        assert not re.search(r"doc_\d+|[a-z]+_[a-z]+", text), text


def test_move_does_not_overwrite_an_earlier_file(env, tmp_path):
    client, _, _ = env()
    for _ in range(2):
        drop(tmp_path, "doc_0200.pdf")
        client.post("/move", json={"filename": "doc_0200.pdf", "status": "auto_approved"}, headers=AUTH)
    assert len(list((tmp_path / "data" / "processed").glob("doc_0200*.pdf"))) == 2


def test_move_rejects_missing_and_unsafe_files(env, tmp_path):
    client, _, _ = env()
    assert client.post("/move", json={"filename": "doc_9999.pdf", "status": "auto_approved"},
                       headers=AUTH).status_code == 404
    (tmp_path / "data").mkdir(exist_ok=True)
    (tmp_path / "secret.pdf").write_bytes(b"x")
    assert client.post("/move", json={"filename": "../secret.pdf", "status": "auto_approved"},
                       headers=AUTH).status_code == 422
    assert (tmp_path / "secret.pdf").exists()
