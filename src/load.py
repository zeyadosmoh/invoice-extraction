"""Load validated extraction results into PostgreSQL (schema `raw`).

    python src/load.py                       # cached results for LLM_MODEL, today's date
    python src/load.py --today 2026-11-08    # pin the reference date of the future-date rule

Uses cached results only (no API calls) and the same code path as src/validate.py, so statuses and
reasons match data/review/queue.csv. One row per invoice from the validator's "scan" inbox view:
the scan where one exists, otherwise the PDF.

Tables (created if missing, then truncated and refilled in one transaction, so re-running replaces the
data, never duplicates it; dbt's staging views on top of them keep working):
    raw.invoices       one row per invoice: normalised header fields, review status, reasons, lineage
    raw.invoice_lines  one row per extracted line item
    raw.review_queue   one row per failed validation rule (only for invoices that need review)

Connection settings come from .env: POSTGRES_USER / POSTGRES_PASSWORD / POSTGRES_DB / POSTGRES_PORT
(host 127.0.0.1).

After changing the table definitions below, run once with --recreate: it drops the raw tables
CASCADE (including dbt's staging views; `dbt build` recreates them).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import psycopg

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract.gemini import DEFAULT_MODEL  # noqa: E402
from validate import (  # noqa: E402
    FAIL,
    ROOT,
    SKIP,
    build_queue,
    inbox_views,
    load_records,
    model_dir_for,
    queue_status,
)

DROP_SQL = "DROP TABLE IF EXISTS raw.review_queue, raw.invoice_lines, raw.invoices CASCADE"

DDL = """
CREATE SCHEMA IF NOT EXISTS raw;

CREATE TABLE IF NOT EXISTS raw.invoices (
    doc_id          text PRIMARY KEY,
    source_file     text NOT NULL,      -- e.g. doc_0071.png (the file the values were extracted from)
    file_type       text NOT NULL,      -- pdf / png
    vendor_name     text,               -- as printed
    vendor_name_ar  text,
    vendor_tax_id   text,
    buyer_name      text,
    invoice_number  text,
    invoice_date    date,
    due_date        date,
    currency        text,
    subtotal        numeric(14, 2),
    discount        numeric(14, 2),
    tax_rate        numeric(6, 3),
    tax_amount      numeric(14, 2),
    total           numeric(14, 2),
    n_line_items    integer NOT NULL,
    status          text NOT NULL,      -- auto_approved / needs_review
    n_failed        integer NOT NULL,
    failed_reasons  text,               -- "rule: reason; rule: reason"
    skipped_rules   text,
    provider        text,
    model           text NOT NULL,
    prompt_version  text,
    validated_as_of date NOT NULL,      -- reference date of the future-date rule
    loaded_at       timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS raw.invoice_lines (
    doc_id      text NOT NULL REFERENCES raw.invoices (doc_id),
    line_no     integer NOT NULL,
    description text,
    quantity    numeric(14, 3),
    unit_price  numeric(14, 2),
    line_total  numeric(14, 2),
    loaded_at   timestamptz NOT NULL,
    PRIMARY KEY (doc_id, line_no)
);

CREATE TABLE IF NOT EXISTS raw.review_queue (
    doc_id    text NOT NULL REFERENCES raw.invoices (doc_id),
    rule      text NOT NULL,
    reason    text NOT NULL,
    loaded_at timestamptz NOT NULL,
    PRIMARY KEY (doc_id, rule)
);
"""


def _str(v) -> str | None:
    return None if v in (None, "") else str(v)


def build_rows(model: str, today: date, loaded_at: datetime) -> tuple[list, list, list]:
    """Return (invoice rows, line rows, review rows) for the scan inbox view."""
    model_dir = model_dir_for(model)
    records = load_records(model_dir)
    if not records:
        raise SystemExit(f"No cached results in {model_dir}. Run src/run_extraction.py first.")
    _, results = build_queue(records, today)

    invoices, lines, review = [], [], []
    for rec in inbox_views(records)["scan"]:
        cache = json.loads((model_dir / f"{rec.doc_key}.json").read_text(encoding="utf-8"))
        inv, res = rec.invoice, results[rec.doc_key]
        failed = [r for r in res if r.status == FAIL]
        skipped = [r for r in res if r.status == SKIP]
        invoices.append((
            rec.doc_id, cache.get("source_file") or rec.doc_key, rec.file_type,
            inv.vendor_name, inv.vendor_name_ar, inv.vendor_tax_id, inv.buyer_name,
            inv.invoice_number, inv.invoice_date, inv.due_date, inv.currency,
            inv.subtotal, inv.discount, inv.tax_rate, inv.tax_amount, inv.total,
            len(inv.line_items), queue_status(res), len(failed),
            _str("; ".join(f"{r.rule}: {r.reason}" for r in failed)),
            _str("; ".join(f"{r.rule}: {r.reason}" for r in skipped)),
            cache.get("provider"), cache.get("model") or model, cache.get("prompt_version"),
            today, loaded_at,
        ))
        for i, li in enumerate(inv.line_items, start=1):
            lines.append((rec.doc_id, i, li.description, li.quantity, li.unit_price, li.line_total, loaded_at))
        for r in failed:
            review.append((rec.doc_id, r.rule, r.reason, loaded_at))
    return invoices, lines, review


def load(conn: psycopg.Connection, invoices: list, lines: list, review: list, recreate: bool = False) -> None:
    """Replace the contents of the raw tables in a single transaction."""
    with conn.transaction(), conn.cursor() as cur:
        if recreate:
            cur.execute(DROP_SQL)
        cur.execute(DDL)
        cur.execute("TRUNCATE raw.review_queue, raw.invoice_lines, raw.invoices")
        cur.executemany(
            "INSERT INTO raw.invoices VALUES (" + ", ".join(["%s"] * 26) + ")", invoices)
        cur.executemany("INSERT INTO raw.invoice_lines VALUES (%s, %s, %s, %s, %s, %s, %s)", lines)
        cur.executemany("INSERT INTO raw.review_queue VALUES (%s, %s, %s, %s)", review)


def connect() -> psycopg.Connection:
    return psycopg.connect(
        host=os.environ.get("POSTGRES_HOST", "127.0.0.1"),
        port=int(os.environ.get("POSTGRES_PORT", "5434")),
        user=os.environ["POSTGRES_USER"],
        password=os.environ["POSTGRES_PASSWORD"],
        dbname=os.environ["POSTGRES_DB"],
    )


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL)
    p.add_argument("--today", type=date.fromisoformat, default=date.today(),
                   help="reference date for the future-date rule (default: today)")
    p.add_argument("--recreate", action="store_true",
                   help="drop the raw tables (CASCADE) and recreate them; needed after a schema change")
    args = p.parse_args(argv)

    loaded_at = datetime.now(timezone.utc)
    invoices, lines, review = build_rows(args.model, args.today, loaded_at)
    with connect() as conn:
        load(conn, invoices, lines, review, recreate=args.recreate)
        with conn.cursor() as cur:
            for t in ("raw.invoices", "raw.invoice_lines", "raw.review_queue"):
                cur.execute(f"SELECT count(*) FROM {t}")
                print(f"{t:<18} {cur.fetchone()[0]:>5} rows")
    n_review = sum(r[17] == "needs_review" for r in invoices)
    print(f"model={args.model} today={args.today}: {len(invoices) - n_review} auto_approved, "
          f"{n_review} needs_review")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
