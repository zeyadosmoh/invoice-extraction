"""Offline tests for the validation rules, review queue and report helpers (no network)."""

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

from extract.schema import ExtractedInvoice  # noqa: E402
from validate import (  # noqa: E402
    AUTO_APPROVED, FAIL, NEEDS_REVIEW, PASS, SKIP, Record, RuleResult, build_queue, check_date_sequence,
    check_due_date, check_duplicates, check_line_totals, check_not_future, check_required_fields,
    check_subtotal, check_tax, check_total, inbox_views, invoice_seq, load_records, queue_row, queue_status,
    validate_inbox, write_queue,
)
from report import Tally, field_group, p95  # noqa: E402

TODAY = date(2026, 11, 8)


def make_invoice(**over) -> ExtractedInvoice:
    """A consistent invoice: 2 x 50.00 + 1 x 20.00 = 120.00, discount 10.00, 5% tax = 5.50, total 115.50."""
    data = {
        "vendor_name": "Example Vendor LLC",
        "invoice_number": "EV-0100",
        "invoice_date": "2026-01-10",
        "due_date": "2026-02-09",
        "currency": "AED",
        "line_items": [
            {"description": "Item A", "quantity": "2", "unit_price": "50.00", "line_total": "100.00"},
            {"description": "Item B", "quantity": "1", "unit_price": "20.00", "line_total": "20.00"},
        ],
        "subtotal": "120.00",
        "discount": "10.00",
        "tax_rate": "5",
        "tax_amount": "5.50",
        "total": "115.50",
    }
    data.update(over)
    return ExtractedInvoice.model_validate(data)


def rec(doc_id: str, file_type: str = "pdf", **over) -> Record:
    return Record(f"{doc_id}.{file_type}", doc_id, file_type, make_invoice(**over))


def test_clean_invoice_passes_every_single_rule():
    inv = make_invoice()
    for check in (check_line_totals, check_subtotal, check_tax, check_total, check_required_fields, check_due_date):
        assert check(inv).status == PASS, check.__name__
    assert check_not_future(inv, TODAY).status == PASS


# ---------------------------------------------------------------- arithmetic

def test_line_total_mismatch_fails_with_reason():
    inv = make_invoice(line_items=[
        {"description": "A", "quantity": "2", "unit_price": "50.00", "line_total": "110.00"},
        {"description": "B", "quantity": "1", "unit_price": "20.00", "line_total": "20.00"},
    ])
    r = check_line_totals(inv)
    assert r.status == FAIL
    assert "line 1" in r.reason and "100.00" in r.reason and "110.00" in r.reason


def test_line_total_within_tolerance_passes():
    inv = make_invoice(line_items=[
        {"description": "A", "quantity": "3", "unit_price": "33.333", "line_total": "100.00"},
    ])
    assert check_line_totals(inv).status == PASS  # 99.999 vs 100.00


def test_line_with_missing_number_fails():
    inv = make_invoice(line_items=[{"description": "A", "quantity": None, "unit_price": "5", "line_total": "5"}])
    r = check_line_totals(inv)
    assert r.status == FAIL and "quantity missing" in r.reason


def test_no_line_items_is_skipped():
    assert check_line_totals(make_invoice(line_items=[])).status == SKIP
    assert check_subtotal(make_invoice(line_items=[])).status == SKIP


def test_subtotal_mismatch_fails():
    r = check_subtotal(make_invoice(subtotal="125.00"))
    assert r.status == FAIL and "120.00" in r.reason and "125.00" in r.reason


def test_tax_uses_subtotal_minus_discount():
    assert check_tax(make_invoice()).status == PASS  # 5% of 110.00 = 5.50
    r = check_tax(make_invoice(tax_amount="6.00"))  # 5% of 120.00: discount ignored
    assert r.status == FAIL and "5.50" in r.reason


def test_tax_without_discount():
    inv = make_invoice(discount=None, tax_amount="6.00", total="126.00")
    assert check_tax(inv).status == PASS
    assert check_total(inv).status == PASS


def test_tax_not_printed_is_skipped():
    inv = make_invoice(tax_rate=None, tax_amount=None, total="110.00")
    assert check_tax(inv).status == SKIP
    assert check_total(inv).status == PASS  # no tax: total = subtotal - discount


def test_total_mismatch_fails():
    r = check_total(make_invoice(total="125.50"))
    assert r.status == FAIL and "115.50" in r.reason and "125.50" in r.reason


def test_total_within_tolerance_passes():
    assert check_total(make_invoice(total="115.51")).status == PASS
    assert check_total(make_invoice(total="115.52")).status == FAIL


def test_negative_discount_from_model_is_neutralised_by_normalisation():
    # parse_invoice / normalize_payload store the discount as a positive amount.
    from extract.schema import parse_invoice

    inv = parse_invoice({**json.loads(make_invoice().model_dump_json()), "discount": "-10.00"})
    assert inv.discount == Decimal("10.00")
    assert check_total(inv).status == PASS


# ---------------------------------------------------------------- required fields

@pytest.mark.parametrize("field", ["invoice_number", "vendor_name", "invoice_date", "total", "currency"])
def test_required_field_missing_fails(field):
    r = check_required_fields(make_invoice(**{field: None}))
    assert r.status == FAIL and field in r.reason


def test_optional_fields_not_required():
    assert check_required_fields(make_invoice(due_date=None, buyer_name=None, tax_rate=None)).status == PASS


# ---------------------------------------------------------------- dates

def test_due_before_invoice_date_fails():
    r = check_due_date(make_invoice(due_date="2026-01-09"))
    assert r.status == FAIL and "2026-01-09" in r.reason


def test_due_same_day_passes():
    assert check_due_date(make_invoice(due_date="2026-01-10")).status == PASS


def test_due_date_missing_is_skipped():
    assert check_due_date(make_invoice(due_date=None)).status == SKIP


def test_future_invoice_date_fails():
    assert check_not_future(make_invoice(invoice_date="2026-11-09", due_date=None), TODAY).status == FAIL
    assert check_not_future(make_invoice(invoice_date="2026-11-08", due_date=None), TODAY).status == PASS
    assert check_not_future(make_invoice(invoice_date=None), TODAY).status == SKIP


# ---------------------------------------------------------------- duplicates

def test_invoice_seq():
    assert invoice_seq("SHF2025-0380") == 380
    assert invoice_seq("RC00814") == 814
    assert invoice_seq("VS-INV-00559") == 559
    assert invoice_seq("NO-DIGITS") is None
    assert invoice_seq(None) is None


def test_duplicate_flags_only_the_later_copy():
    a = rec("doc_0001", invoice_number="EV-0100")
    b = rec("doc_0002", invoice_number="EV-0101")
    c = rec("doc_0003", invoice_number="EV-0100")
    out = check_duplicates([a, b, c])
    assert out[a.doc_key].status == PASS
    assert out[b.doc_key].status == PASS
    assert out[c.doc_key].status == FAIL and "doc_0001" in out[c.doc_key].reason


def test_duplicate_is_per_vendor_and_ignores_case_and_spacing():
    a = rec("doc_0001", vendor_name="Example  Vendor LLC")
    b = rec("doc_0002", vendor_name="example vendor llc")
    other = rec("doc_0003", vendor_name="Another Vendor")
    out = check_duplicates([a, b, other])
    assert out[b.doc_key].status == FAIL
    assert out[other.doc_key].status == PASS  # same number, different vendor


def test_duplicate_skipped_without_number():
    r = rec("doc_0001", invoice_number=None)
    assert check_duplicates([r])[r.doc_key].status == SKIP


# ---------------------------------------------------------------- date sequence

def _seq(*rows) -> list[Record]:
    return [rec(f"doc_{i:04d}", invoice_number=f"EV-{n:04d}", invoice_date=d, due_date=None)
            for i, (n, d) in enumerate(rows, start=1)]


def test_sequence_in_order_passes():
    recs = _seq((100, "2026-01-05"), (101, "2026-01-05"), (102, "2026-02-01"))
    assert all(r.status == PASS for r in check_date_sequence(recs).values())


def test_day_month_swap_flags_only_the_swapped_invoice():
    # 2026-02-01 misread as 2026-01-02 is earlier than both lower-numbered invoices.
    recs = _seq((100, "2026-01-10"), (101, "2026-01-20"), (102, "2026-01-02"), (103, "2026-02-15"))
    out = check_date_sequence(recs)
    assert out["doc_0003.pdf"].status == FAIL
    assert "day/month swap" in out["doc_0003.pdf"].reason
    assert [out[f"doc_000{i}.pdf"].status for i in (1, 2, 4)] == [PASS, PASS, PASS]


def test_swap_forward_flags_only_the_swapped_invoice():
    # 2026-01-03 misread as 2026-03-01: later than two higher-numbered invoices.
    recs = _seq((100, "2026-01-01"), (101, "2026-03-01"), (102, "2026-01-15"), (103, "2026-02-01"))
    out = check_date_sequence(recs)
    assert out["doc_0002.pdf"].status == FAIL
    assert [out[f"doc_000{i}.pdf"].status for i in (1, 3, 4)] == [PASS, PASS, PASS]


def test_single_conflicting_pair_flags_both():
    recs = _seq((100, "2026-02-01"), (101, "2026-01-01"))
    out = check_date_sequence(recs)
    assert out["doc_0001.pdf"].status == FAIL and out["doc_0002.pdf"].status == FAIL


def test_swap_that_stays_in_order_is_not_detectable():
    # The doc_0071 case: the misread date still sits between its neighbours.
    recs = _seq((100, "2025-12-08"), (101, "2026-03-01"), (102, "2026-06-21"))
    assert all(r.status == PASS for r in check_date_sequence(recs).values())


def test_sequence_is_per_vendor():
    a = rec("doc_0001", invoice_number="EV-0200", invoice_date="2026-03-01", due_date=None)
    b = rec("doc_0002", vendor_name="Other Vendor", invoice_number="OV-0100", invoice_date="2026-01-01", due_date=None)
    out = check_date_sequence([a, b])
    assert out[a.doc_key].status == SKIP and out[b.doc_key].status == SKIP  # each is its vendor's only invoice


def test_sequence_skips_missing_date_and_excluded():
    recs = _seq((100, "2026-01-01"), (101, None), (102, "2026-02-01"))
    out = check_date_sequence(recs, exclude={"doc_0003.pdf"})
    assert out["doc_0002.pdf"].status == SKIP
    assert out["doc_0003.pdf"].status == SKIP and "duplicate" in out["doc_0003.pdf"].reason


def test_duplicate_does_not_cause_sequence_false_alarm():
    # doc_0003 reuses an old number with a new date: flagged as duplicate, and its neighbour stays clean.
    recs = _seq((100, "2026-01-01"), (101, "2026-02-01"), (100, "2026-03-01"))
    res = validate_inbox(recs, TODAY)
    status = {k: {r.rule: r.status for r in v} for k, v in res.items()}
    assert status["doc_0003.pdf"]["duplicate"] == FAIL
    assert status["doc_0003.pdf"]["date_sequence"] == SKIP
    assert status["doc_0002.pdf"]["date_sequence"] == PASS


# ---------------------------------------------------------------- queue

def test_skip_counts_as_pass_for_status():
    results = [RuleResult("tax", SKIP, "no tax printed"), RuleResult("total", PASS)]
    assert queue_status(results) == AUTO_APPROVED
    assert queue_status(results + [RuleResult("subtotal", FAIL, "x")]) == NEEDS_REVIEW


def test_queue_row_lists_all_failures_and_skips_separately():
    results = [
        RuleResult("tax", FAIL, "tax wrong"),
        RuleResult("total", FAIL, "total wrong"),
        RuleResult("due_date", SKIP, "invoice date or due date missing"),
        RuleResult("subtotal", PASS),
    ]
    row = queue_row(rec("doc_0001"), results)
    assert row["status"] == NEEDS_REVIEW
    assert row["n_failed"] == 2
    assert row["failed_reasons"] == "tax: tax wrong; total: total wrong"
    assert row["skipped_rules"] == "due_date: invoice date or due date missing"


def test_inbox_views_scan_replaces_its_own_pdf():
    recs = {r.doc_key: r for r in [rec("doc_0001"), rec("doc_0001", "png"), rec("doc_0002")]}
    views = inbox_views(recs)
    assert [r.doc_key for r in views["pdf"]] == ["doc_0001.pdf", "doc_0002.pdf"]
    assert [r.doc_key for r in views["scan"]] == ["doc_0001.png", "doc_0002.pdf"]


def test_scan_is_not_a_duplicate_of_its_own_pdf():
    recs = {r.doc_key: r for r in [rec("doc_0001"), rec("doc_0001", "png")]}
    rows, _ = build_queue(recs, TODAY)
    assert [r["status"] for r in rows] == [AUTO_APPROVED, AUTO_APPROVED]


def test_missing_invoice_date_goes_to_review():
    recs = {r.doc_key: r for r in [rec("doc_0001", invoice_date=None)]}
    rows, _ = build_queue(recs, TODAY)
    assert rows[0]["status"] == NEEDS_REVIEW
    assert "required_fields: missing: invoice_date" in rows[0]["failed_reasons"]
    assert "due_date" in rows[0]["skipped_rules"]


def test_load_records_reparses_raw_response_and_writes_queue(tmp_path):
    inv = json.loads(make_invoice().model_dump_json())
    raw = {**inv, "discount": "-10.00"}  # what the model said
    cached = {"doc_id": "doc_0001", "invoice": {**inv, "discount": "-10.00"}, "raw_response": json.dumps(raw),
              "input_tokens": 10, "output_tokens": 5, "latency_s": 1.0}
    (tmp_path / "doc_0001.pdf.json").write_text(json.dumps(cached), encoding="utf-8")
    records = load_records(tmp_path)
    r = records["doc_0001.pdf"]
    assert r.invoice.discount == Decimal("10.00")  # current normalisation applied
    assert r.extra["cached_invoice"]["discount"] == "-10.00"  # stored output kept for "before fix"
    rows, _ = build_queue(records, TODAY)
    out = tmp_path / "review" / "queue.csv"
    write_queue(rows, out)
    with out.open(encoding="utf-8") as fh:
        got = list(csv.DictReader(fh))
    assert got[0]["doc_key"] == "doc_0001.pdf" and got[0]["status"] == AUTO_APPROVED
    assert "skipped_rules" in got[0]


# ---------------------------------------------------------------- report helpers

def test_p95_nearest_rank():
    assert p95([float(i) for i in range(1, 21)]) == 19.0
    assert p95([3.0]) == 3.0


def test_pct_never_rounds_up_to_100():
    t = Tally()
    for ok in [True] * 4990 + [False] * 2:
        t.add(ok)
    assert t.pct() == "99.9%"
    perfect = Tally()
    perfect.add(True)
    assert perfect.pct() == "100%"


def test_field_group():
    assert field_group("line_items[3].unit_price") == "line_items.unit_price"
    assert field_group("total") == "total"


# ---------------------------------------------------------------- single file vs everything stored

def test_validate_against_flags_a_new_duplicate_of_a_stored_invoice():
    from validate import validate_against

    existing = [rec("doc_0001"), rec("doc_0002", invoice_number="EV-0101", invoice_date="2026-01-20")]
    res = {r.rule: r for r in validate_against(rec("doc_0300"), existing, TODAY)}
    assert res["duplicate"].status == FAIL and "(doc_0001)" in res["duplicate"].reason
    assert res["date_sequence"].status == SKIP  # a duplicate is not compared for order


def test_validate_against_keeps_a_resent_invoice_in_its_original_place():
    """Re-processing the first copy of a duplicate pair must not flag it against the later copy."""
    from validate import validate_against

    existing = [rec("doc_0001"), rec("doc_0002", invoice_number="EV-0101", invoice_date="2026-01-20"),
                rec("doc_0009")]  # doc_0009 reuses EV-0100: the later copy
    first = {r.rule: r for r in validate_against(rec("doc_0001", "png"), existing, TODAY)}
    later = {r.rule: r for r in validate_against(rec("doc_0009"), existing, TODAY)}
    assert first["duplicate"].status == PASS
    assert later["duplicate"].status == FAIL


# ---------------------------------------------------------------- plain-English reasons (dashboard wording)

def test_plain_english_matches_dashboard_wording():
    from validate import check_date_sequence as _seq, plain_english

    cases = [
        (check_tax(make_invoice(tax_amount="9.99")),
         ("Tax amount wrong", "Tax should be 5.50 (5% of 110.00) but 9.99 is printed")),
        (check_total(make_invoice(total="1234.00")),
         ("Total doesn't add up", "Total should be 115.50 (subtotal - discount + tax) but 1,234.00 is printed")),
        (check_subtotal(make_invoice(subtotal="1500.00")),
         ("Subtotal doesn't add up", "Subtotal should be 120.00 (sum of the lines) but 1,500.00 is printed")),
        (check_required_fields(make_invoice(invoice_date=None)),
         ("Missing invoice date", "Invoice date is not printed on the document")),
        (check_required_fields(make_invoice(invoice_date=None, invoice_number=None)),
         ("Missing invoice number, invoice date", "Invoice number, invoice date are not printed on the document")),
        (check_due_date(make_invoice(due_date="2026-01-01")),
         ("Due date before invoice date", "Due date 2026-01-01 is before invoice date 2026-01-10")),
        (check_not_future(make_invoice(invoice_date="2027-01-01", due_date="2027-02-01"), TODAY),
         ("Invoice date in the future", "Invoice date 2027-01-01 is in the future")),
        (check_line_totals(make_invoice(line_items=[{"quantity": "2", "unit_price": "5", "line_total": "11"}])),
         ("Line total wrong", "One or more lines: quantity x unit price does not match the line amount")),
    ]
    dups = check_duplicates([rec("doc_0001", invoice_date="2025-02-07"), rec("doc_0002")])
    cases.append((dups["doc_0002.pdf"],
                  ("Duplicate invoice number", "Same invoice number as an invoice dated 2025-02-07")))
    seq = _seq([rec("doc_0001", invoice_number="EV-1", invoice_date="2026-01-01"),
                rec("doc_0002", invoice_number="EV-2", invoice_date="2026-09-01"),
                rec("doc_0003", invoice_number="EV-3", invoice_date="2026-01-03"),
                rec("doc_0004", invoice_number="EV-4", invoice_date="2026-01-04")])
    cases.append((seq["doc_0002.pdf"],
                  ("Invoice date out of sequence", "Invoice date is out of order with this vendor's other invoices")))
    for result, expected in cases:
        assert result.status == FAIL, result
        assert plain_english(result) == expected
