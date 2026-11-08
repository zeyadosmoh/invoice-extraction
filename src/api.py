"""Local pipeline API: process one invoice file end to end, and move files out of the inbox.

    python src/api.py                 # serves on 127.0.0.1:8765 (PIPELINE_API_HOST / PIPELINE_API_PORT)

Used by the n8n workflow (n8n/workflows/invoice_pipeline.json), but any client can call it.

Endpoints (all except /health need `Authorization: Bearer <PIPELINE_API_TOKEN>` from .env):
    GET  /health   liveness + database check
    POST /process  multipart upload `file` (PDF or PNG): extract (cached as usual), normalise,
                   validate against every invoice already in Postgres, upsert by doc_id, and return
                   the status with the failed reasons in plain English
    POST /move     JSON body = the /process response (or {filename, status, ...}): moves
                   data/inbox/<filename> to data/processed/ (auto_approved) or data/needs_review/
                   (anything else) and, for the latter, appends a row to data/needs_review/review_log.csv

Settings (.env): PIPELINE_API_TOKEN (required), PIPELINE_CACHE_ONLY=1 to refuse files that would need
an LLM call (returns 409), plus the usual LLM_* and POSTGRES_* settings.
Files are processed one at a time, so overlapping polls never extract the same file twice.
"""

from __future__ import annotations

import csv
import hmac
import json
import os
import re
import shutil
import sys
import tempfile
import threading
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Protocol

from fastapi import Depends, FastAPI, File, Header, HTTPException, UploadFile
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import get_extractor  # noqa: E402
from extract.base import ConfigError, Extractor  # noqa: E402
from load import rows_for_record  # noqa: E402
from run_extraction import DEFAULT_OUT, cache_path, cached_result_is_valid, run, safe_dirname, sha256_file  # noqa: E402
from validate import (  # noqa: E402
    AUTO_APPROVED,
    FAIL,
    ROOT,
    RULE_LABELS,
    Record,
    load_record,
    queue_status,
    validate_against,
)

DATA = ROOT / "data"
INBOX, PROCESSED, NEEDS_REVIEW_DIR = DATA / "inbox", DATA / "processed", DATA / "needs_review"
REVIEW_LOG_COLUMNS = ("logged_at", "filename", "doc_id", "status", "vendor", "invoice_number", "total",
                      "currency", "reasons")
FILENAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,120}\.(pdf|png)$", re.IGNORECASE)


class Store(Protocol):
    """Where processed invoices live. Postgres in production, a fake in tests."""

    def existing_records(self) -> list[Record]: ...
    def upsert(self, invoice: tuple, lines: list, review: list) -> None: ...
    def ping(self) -> None: ...


class PostgresStore:
    def _conn(self):
        from load import connect

        return connect()

    def existing_records(self) -> list[Record]:
        from load import fetch_existing_records

        with self._conn() as conn:
            return fetch_existing_records(conn)

    def upsert(self, invoice: tuple, lines: list, review: list) -> None:
        from load import upsert_invoice

        with self._conn() as conn:
            upsert_invoice(conn, invoice, lines, review)

    def ping(self) -> None:
        with self._conn() as conn, conn.cursor() as cur:
            cur.execute("SELECT 1")


class Reason(BaseModel):
    rule: str
    issue: str
    detail: str


class ProcessResult(BaseModel):
    filename: str
    doc_id: str = ""
    status: str
    failed_reasons: list[Reason] = []
    vendor: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    total: str | None = None
    currency: str | None = None
    cached: bool | None = None


class MoveResult(BaseModel):
    filename: str
    moved_to: str
    logged: bool


def check_filename(name: str | None) -> str:
    """Accept a bare file name like doc_0001.pdf: no paths, PDF or PNG only."""
    name = (name or "").strip()
    if not FILENAME_RE.fullmatch(name):
        raise HTTPException(422, "filename must be a plain name ending in .pdf or .png "
                                 "(letters, digits, '.', '_' and '-' only)")
    return name


def reasons_text(reasons: list[Reason]) -> str:
    return "; ".join(f"{r.issue}: {r.detail}" for r in reasons)


def create_app(
    *,
    token: str | None = None,
    store: Store | None = None,
    extractor_factory: Callable[[], Extractor] = get_extractor,
    cache_root: Path = DEFAULT_OUT,
    data_dir: Path = DATA,
    cache_only: bool | None = None,
    today: Callable[[], date] = date.today,
) -> FastAPI:
    token = token if token is not None else os.environ.get("PIPELINE_API_TOKEN", "")
    if not token or token.startswith("change_me") or len(token) < 16:
        raise ConfigError("PIPELINE_API_TOKEN is not set (or shorter than 16 characters) in .env")
    if cache_only is None:
        cache_only = os.environ.get("PIPELINE_CACHE_ONLY", "").strip().lower() in ("1", "true", "yes")
    store = store or PostgresStore()
    inbox, processed, needs_review = data_dir / "inbox", data_dir / "processed", data_dir / "needs_review"
    lock = threading.Lock()
    extractor_holder: list[Extractor] = []

    def extractor() -> Extractor:
        if not extractor_holder:
            extractor_holder.append(extractor_factory())
        return extractor_holder[0]

    def require_token(authorization: str | None = Header(default=None)) -> None:
        scheme, _, given = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not hmac.compare_digest(given.strip().encode(), token.encode()):
            raise HTTPException(401, "missing or invalid token", headers={"WWW-Authenticate": "Bearer"})

    app = FastAPI(title="Invoice pipeline API", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/health")
    def health() -> dict:
        try:
            store.ping()
            db = "ok"
        except Exception as e:  # noqa: BLE001 - report, don't crash
            db = f"error: {e.__class__.__name__}"
        return {"ok": db == "ok", "db": db}

    @app.post("/process", response_model=ProcessResult, dependencies=[Depends(require_token)])
    def process(file: UploadFile = File(...)) -> ProcessResult:
        filename = check_filename(file.filename)
        with lock, tempfile.TemporaryDirectory(prefix="invoice_api_") as tmp:
            doc = Path(tmp) / filename
            with doc.open("wb") as fh:
                shutil.copyfileobj(file.file, fh)
            if doc.stat().st_size == 0:
                raise HTTPException(422, "empty file")

            ex = extractor()
            out_dir = cache_root / safe_dirname(ex.model)
            cache = cache_path(out_dir, doc)
            cached = cache.exists() and cached_result_is_valid(cache, sha256_file(doc))
            if cache_only and not cached:
                raise HTTPException(409, f"{filename} has no cached extraction and PIPELINE_CACHE_ONLY is on")

            log: list[str] = []
            summary = run(ex, Path(tmp), cache_root, delay_s=0, log=log.append)
            if summary.stopped_reason:
                raise HTTPException(503, summary.stopped_reason)
            if summary.failed:
                raise HTTPException(502, f"extraction failed for {filename}: {log[-1].strip() if log else ''}")

        rec = load_record(cache)
        results = validate_against(rec, store.existing_records(), today())
        cache_json = json.loads(cache.read_text(encoding="utf-8"))
        invoice, lines, review = rows_for_record(rec, results, cache_json, ex.model, today(),
                                                 datetime.now(timezone.utc))
        store.upsert(invoice, lines, review)

        inv = rec.invoice
        return ProcessResult(
            filename=filename,
            doc_id=rec.doc_id,
            status=queue_status(results),
            failed_reasons=[Reason(rule=r.rule, issue=RULE_LABELS.get(r.rule, r.rule), detail=r.reason)
                            for r in results if r.status == FAIL],
            vendor=inv.vendor_name,
            invoice_number=inv.invoice_number,
            invoice_date=inv.invoice_date.isoformat() if inv.invoice_date else None,
            total=None if inv.total is None else f"{inv.total:.2f}",
            currency=inv.currency,
            cached=cached,
        )

    @app.post("/move", response_model=MoveResult, dependencies=[Depends(require_token)])
    def move(body: ProcessResult) -> MoveResult:
        filename = check_filename(body.filename)
        src = inbox / filename
        if not src.is_file():
            raise HTTPException(404, f"{filename} is not in the inbox")
        dest_dir = processed if body.status == AUTO_APPROVED else needs_review
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / filename
        if dest.exists():  # keep the earlier copy; add a timestamp to the new one
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
            dest = dest_dir / f"{src.stem}_{stamp}{src.suffix}"
        with lock:
            shutil.move(str(src), str(dest))
            logged = False
            if dest_dir == needs_review:
                log_path = needs_review / "review_log.csv"
                new = not log_path.exists()
                with log_path.open("a", newline="", encoding="utf-8") as fh:
                    w = csv.DictWriter(fh, fieldnames=REVIEW_LOG_COLUMNS)
                    if new:
                        w.writeheader()
                    w.writerow({
                        "logged_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                        "filename": dest.name, "doc_id": body.doc_id, "status": body.status,
                        "vendor": body.vendor or "", "invoice_number": body.invoice_number or "",
                        "total": body.total or "", "currency": body.currency or "",
                        "reasons": reasons_text(body.failed_reasons),
                    })
                logged = True
        return MoveResult(filename=filename, moved_to=str(dest.relative_to(data_dir.parent)), logged=logged)

    return app


def main() -> int:
    import uvicorn
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    try:
        app = create_app()
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 3
    host = os.environ.get("PIPELINE_API_HOST", "127.0.0.1")
    port = int(os.environ.get("PIPELINE_API_PORT", "8765"))
    uvicorn.run(app, host=host, port=port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
