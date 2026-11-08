"""Loader idempotency against the local invoice_pg. Skipped when the database or the cache is missing."""

import os
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

psycopg = pytest.importorskip("psycopg")
from dotenv import load_dotenv  # noqa: E402

import load  # noqa: E402

TABLES = ("raw.invoices", "raw.invoice_lines", "raw.review_queue")


@pytest.fixture(scope="module")
def conn():
    load_dotenv(ROOT / ".env")
    try:
        c = load.connect()
    except (KeyError, psycopg.OperationalError) as exc:
        pytest.skip(f"invoice_pg not reachable: {exc.__class__.__name__}")
    yield c
    c.close()


def _counts(conn) -> dict[str, int]:
    with conn.cursor() as cur:
        out = {}
        for t in TABLES:
            cur.execute(f"SELECT count(*) FROM {t}")
            out[t] = cur.fetchone()[0]
        return out


def test_loading_twice_replaces_rows(conn):
    model = os.environ.get("LLM_MODEL", "").strip() or load.DEFAULT_MODEL
    if not load.model_dir_for(model).exists():
        pytest.skip("no cached extraction results")
    rows = load.build_rows(model, date(2026, 11, 8), datetime.now(timezone.utc))
    load.load(conn, *rows)
    first = _counts(conn)
    load.load(conn, *rows)
    assert _counts(conn) == first == {
        "raw.invoices": len(rows[0]), "raw.invoice_lines": len(rows[1]), "raw.review_queue": len(rows[2]),
    }
    assert len(rows[0]) == len({r[0] for r in rows[0]})  # one row per invoice
