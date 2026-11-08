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


def _table_snapshot(conn) -> dict[str, list]:
    with conn.cursor() as cur:
        out = {}
        for t, key in (("raw.invoices", "doc_id"), ("raw.invoice_lines", "doc_id, line_no"),
                       ("raw.review_queue", "doc_id, rule")):
            cur.execute(f"SELECT * FROM {t} ORDER BY {key}")
            out[t] = cur.fetchall()
        return out


def test_upsert_one_invoice_leaves_the_others_alone(conn):
    """Runs inside a transaction that is rolled back, so the loaded data is not changed."""
    with conn.cursor() as cur:
        cur.execute("SELECT doc_id FROM raw.invoices ORDER BY doc_id LIMIT 1")
        row = cur.fetchone()
    if row is None:
        pytest.skip("raw.invoices is empty")
    doc_id = row[0]
    before = _table_snapshot(conn)
    with conn.transaction(force_rollback=True):
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM raw.invoices WHERE doc_id = %s", (doc_id,))
            inv = list(cur.fetchone())
        inv[15] = 999.99  # total
        lines = [(doc_id, 1, "only line", 1, 999.99, 999.99, inv[25])]
        review = [(doc_id, "total", "test reason", inv[25])]
        load.upsert_invoice(conn, tuple(inv), lines, review)
        load.upsert_invoice(conn, tuple(inv), lines, review)  # idempotent
        after = _table_snapshot(conn)

        # same invoice count, the target row replaced, every other row identical
        assert len(after["raw.invoices"]) == len(before["raw.invoices"])
        others = lambda rows: [r for r in rows if r[0] != doc_id]  # noqa: E731
        for t in TABLES:
            assert others(after[t]) == others(before[t]), t
        target = [r for r in after["raw.invoices"] if r[0] == doc_id][0]
        assert float(target[15]) == 999.99
        assert [r[:3] for r in after["raw.invoice_lines"] if r[0] == doc_id] == [(doc_id, 1, "only line")]
        assert [r[:2] for r in after["raw.review_queue"] if r[0] == doc_id] == [(doc_id, "total")]

        # a brand-new doc_id adds exactly one invoice
        inv[0] = "zz_test_upsert"
        load.upsert_invoice(conn, tuple(inv), [], [])
        assert len(_table_snapshot(conn)["raw.invoices"]) == len(before["raw.invoices"]) + 1
    assert _table_snapshot(conn) == before


def test_fetch_existing_records_reads_every_invoice(conn):
    records = load.fetch_existing_records(conn)
    assert len(records) == _counts(conn)["raw.invoices"]
    assert [r.doc_id for r in records] == sorted(r.doc_id for r in records)
