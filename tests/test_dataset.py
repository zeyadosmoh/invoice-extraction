"""Checks on the generated synthetic dataset (data/synthetic) and the committed samples (data/samples).

The arithmetic here is re-implemented independently of the generator on purpose.
Run `python src/generate_invoices.py` first; dataset tests are skipped if it has not been generated.
"""

from __future__ import annotations

import csv
import json
import re
import sys
from collections import Counter
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "synthetic"
SAMPLES = ROOT / "data" / "samples"
TEMPLATES = {"T1_classic", "T2_modern", "T3_receipt", "T4_two_column", "T5_bilingual_ar_en"}
ANOMALY_TYPES = {"total_mismatch", "missing_invoice_date", "duplicate_invoice_number", "tax_amount_mismatch"}
CURRENCIES = {"USD", "EUR", "SAR", "AED", "EGP"}

D = Decimal


def q2(x: Decimal) -> Decimal:
    return x.quantize(D("0.01"), rounding=ROUND_HALF_UP)


def load_gts(gt_dir: Path) -> list[dict]:
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(gt_dir.glob("doc_*.json"))]


@pytest.fixture(scope="session")
def gts() -> list[dict]:
    items = load_gts(DATA / "ground_truth")
    if not items or not any((DATA / "pdf").glob("doc_*.pdf")):
        pytest.skip("dataset not generated; run: python src/generate_invoices.py")
    return items


@pytest.fixture(scope="session")
def normal(gts) -> list[dict]:
    return [g for g in gts if not g["is_anomaly"]]


def base_and_expected(inv: dict) -> tuple[Decimal, Decimal, Decimal]:
    """(taxable base, expected tax, expected total) from the printed subtotal/discount/rate."""
    base = D(inv["subtotal"]) - D(inv["discount"] or "0")
    tax = q2(base * D(inv["tax_rate"]) / 100)
    return base, tax, base + D(inv["tax_amount"])


# ---------------------------------------------------------------- dataset shape


def test_dataset_size_and_coverage(gts):
    assert 100 <= len(gts) <= 140
    assert {g["meta"]["template"] for g in gts} == TEMPLATES
    assert {g["invoice"]["currency"] for g in gts} == CURRENCIES
    assert len({g["meta"]["vendor_id"] for g in gts}) == 15
    per_template = Counter(g["meta"]["template"] for g in gts)
    assert max(per_template.values()) - min(per_template.values()) <= 2


def test_scan_ratio(gts):
    ratio = sum(1 for g in gts if g["meta"]["files"]["png"]) / len(gts)
    assert 0.25 <= ratio <= 0.35


def test_anomaly_ratio_and_types(gts):
    anomalies = [g for g in gts if g["is_anomaly"]]
    assert 0.08 <= len(anomalies) / len(gts) <= 0.12
    assert {a["anomaly"]["type"] for a in anomalies} == ANOMALY_TYPES
    for g in gts:
        assert g["is_anomaly"] == (g["anomaly"] is not None)
        if g["anomaly"]:
            assert g["anomaly"]["reason"].strip()


def test_line_item_count_range(gts):
    counts = [len(g["invoice"]["line_items"]) for g in gts]
    assert min(counts) >= 1 and max(counts) <= 12
    assert max(counts) == 12, "at least one invoice should use the full 12 lines"


# ---------------------------------------------------------------- files


def test_every_pdf_has_ground_truth_and_vice_versa(gts):
    pdf_ids = {p.stem for p in (DATA / "pdf").glob("doc_*.pdf")}
    gt_ids = {g["doc_id"] for g in gts}
    assert pdf_ids == gt_ids


def test_referenced_files_exist(gts):
    png_ids = {p.stem for p in (DATA / "scans").glob("doc_*.png")}
    for g in gts:
        files = g["meta"]["files"]
        assert (DATA / files["pdf"]).is_file()
        if files["png"]:
            assert (DATA / files["png"]).is_file()
    assert png_ids == {g["doc_id"] for g in gts if g["meta"]["files"]["png"]}, "orphan PNGs on disk"


def test_manifest_matches_ground_truth(gts):
    with open(DATA / "manifest.csv", encoding="utf-8") as fh:
        rows = {r["doc_id"]: r for r in csv.DictReader(fh)}
    assert set(rows) == {g["doc_id"] for g in gts}
    for g in gts:
        r = rows[g["doc_id"]]
        assert r["total"] == g["invoice"]["total"]
        assert r["digits"] == g["meta"]["digits"]
        assert r["template"] == g["meta"]["template"]
        assert r["anomaly_type"] == (g["anomaly"] or {}).get("type", "")


# ---------------------------------------------------------------- normal invoices add up


def test_normal_invoices_add_up(normal):
    assert normal
    for g in normal:
        inv = g["invoice"]
        for ln in inv["line_items"]:
            assert q2(D(ln["quantity"]) * D(ln["unit_price"])) == D(ln["line_total"]), g["doc_id"]
        assert sum(D(ln["line_total"]) for ln in inv["line_items"]) == D(inv["subtotal"]), g["doc_id"]
        base, expected_tax, _ = base_and_expected(inv)
        assert D(inv["tax_amount"]) == expected_tax, g["doc_id"]
        assert D(inv["total"]) == base + expected_tax, g["doc_id"]
        if inv["discount"] is not None:
            assert D("0") < D(inv["discount"]) < D(inv["subtotal"]), g["doc_id"]


def test_normal_invoices_have_dates(normal):
    for g in normal:
        inv = g["invoice"]
        assert inv["invoice_date"] is not None, g["doc_id"]
        assert inv["due_date"] >= inv["invoice_date"], g["doc_id"]


def test_normal_invoice_numbers_unique_per_vendor(gts):
    seen: dict[tuple[str, str], str] = {}
    for g in gts:  # doc ids are chronological
        key = (g["invoice"]["vendor_name"], g["invoice"]["invoice_number"])
        if g["anomaly"] and g["anomaly"]["type"] == "duplicate_invoice_number":
            continue
        assert key not in seen, f"{g['doc_id']} unexpectedly duplicates {seen[key]}"
        seen[key] = g["doc_id"]


def test_money_values_have_two_decimals(gts):
    money = re.compile(r"^-?\d+\.\d{2}$")
    for g in gts:
        inv = g["invoice"]
        for k in ("subtotal", "tax_amount", "total"):
            assert money.match(inv[k]), (g["doc_id"], k)
        for ln in inv["line_items"]:
            assert money.match(ln["unit_price"]) and money.match(ln["line_total"]), g["doc_id"]


# ---------------------------------------------------------------- anomalies really are anomalous


def anomalies_of(gts, atype):
    items = [g for g in gts if g["anomaly"] and g["anomaly"]["type"] == atype]
    assert items, f"no {atype} anomalies"
    return items


def test_total_mismatch_is_real(gts):
    for g in anomalies_of(gts, "total_mismatch"):
        inv = g["invoice"]
        base = D(inv["subtotal"]) - D(inv["discount"] or "0")
        assert D(inv["total"]) != base + D(inv["tax_amount"]), g["doc_id"]


def test_tax_amount_mismatch_is_real(gts):
    for g in anomalies_of(gts, "tax_amount_mismatch"):
        inv = g["invoice"]
        base, expected_tax, consistent_total = base_and_expected(inv)
        assert D(inv["tax_amount"]) != expected_tax, g["doc_id"]
        assert D(inv["total"]) == consistent_total, "only the tax should be wrong"


def test_missing_invoice_date_is_real(gts):
    for g in anomalies_of(gts, "missing_invoice_date"):
        assert g["invoice"]["invoice_date"] is None, g["doc_id"]


def test_duplicate_invoice_number_is_real(gts):
    by_id = {g["doc_id"]: g for g in gts}
    for g in anomalies_of(gts, "duplicate_invoice_number"):
        inv = g["invoice"]
        earlier = [x for x in gts if x["doc_id"] < g["doc_id"]
                   and x["invoice"]["vendor_name"] == inv["vendor_name"]
                   and x["invoice"]["invoice_number"] == inv["invoice_number"]]
        assert earlier, g["doc_id"]
        assert g["anomaly"]["duplicate_of"] in by_id


def test_only_missing_date_anomalies_lack_a_date(gts):
    for g in gts:
        if g["invoice"]["invoice_date"] is None:
            assert g["anomaly"] and g["anomaly"]["type"] == "missing_invoice_date", g["doc_id"]


# ---------------------------------------------------------------- bilingual / digits


def test_digits_field(gts):
    t5 = [g for g in gts if g["meta"]["template"] == "T5_bilingual_ar_en"]
    for g in gts:
        assert g["meta"]["digits"] in {"western", "arabic_indic"}
        if g["meta"]["template"] != "T5_bilingual_ar_en":
            assert g["meta"]["digits"] == "western", g["doc_id"]
    arabic = sum(1 for g in t5 if g["meta"]["digits"] == "arabic_indic")
    assert abs(arabic - len(t5) / 2) <= 1


def test_ground_truth_uses_western_digits_only(gts):
    arabic_indic = re.compile("[٠-٩٫٬]")
    for g in gts:
        inv = g["invoice"]
        numeric = [inv["subtotal"], inv["tax_amount"], inv["total"], inv["tax_rate"],
                   inv["invoice_date"] or "", inv["due_date"], inv["discount"] or ""]
        numeric += [ln[k] for ln in inv["line_items"] for k in ("quantity", "unit_price", "line_total")]
        assert not any(arabic_indic.search(x) for x in numeric), g["doc_id"]


def test_bilingual_invoices_have_arabic_names(gts):
    for g in gts:
        if g["meta"]["template"] == "T5_bilingual_ar_en":
            assert g["invoice"]["vendor_name_ar"], g["doc_id"]
            assert all(ln["description_ar"] for ln in g["invoice"]["line_items"]), g["doc_id"]


# ---------------------------------------------------------------- committed samples (no generation needed)


def test_samples_cover_every_template_and_two_scans():
    pdfs = sorted(SAMPLES.glob("doc_*.pdf"))
    pngs = sorted(SAMPLES.glob("doc_*.png"))
    assert len(pdfs) == 5 and len(pngs) == 2
    templates = set()
    for f in pdfs + pngs:
        gt_path = SAMPLES / f"{f.stem}.json"
        assert gt_path.is_file(), f"missing ground truth for sample {f.name}"
        gt = json.loads(gt_path.read_text(encoding="utf-8"))
        if f.suffix == ".pdf":
            templates.add(gt["meta"]["template"])
    assert templates == TEMPLATES
    readme = (SAMPLES / "README.md").read_text(encoding="utf-8")
    for link in re.findall(r"\]\(([^)]+)\)", readme):
        assert (SAMPLES / link).is_file(), f"broken link in samples README: {link}"


# ---------------------------------------------------------------- reproducibility


def test_generator_is_reproducible(tmp_path):
    sys.path.insert(0, str(ROOT / "src"))
    import generate_invoices as gen

    a, b = tmp_path / "a", tmp_path / "b"
    gen.generate(a, seed=7, n=15, samples_dir=None)
    gen.generate(b, seed=7, n=15, samples_dir=None)
    files_a = sorted(p.relative_to(a) for p in a.rglob("*") if p.is_file())
    files_b = sorted(p.relative_to(b) for p in b.rglob("*") if p.is_file())
    assert files_a == files_b and files_a
    for rel in files_a:
        assert (a / rel).read_bytes() == (b / rel).read_bytes(), f"non-deterministic output: {rel}"

    gen.generate(b, seed=8, n=15, samples_dir=None)
    assert (a / "manifest.csv").read_bytes() != (b / "manifest.csv").read_bytes()
