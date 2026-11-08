"""Validation rules and the human review queue.

    python src/validate.py                          # cached results for LLM_MODEL -> data/review/queue.csv
    python src/validate.py --model gemini-3.5-flash-lite --today 2026-11-08

Works only from cached extraction results: no API calls. Each cached result is rebuilt from the
model's `raw_response` through `parse_invoice`, so the current normalisation rules (e.g. the
discount sign fix) apply without re-running the model.

Every rule returns pass, fail (with a reason) or skip (the rule could not run, e.g. no tax printed).
A document is `auto_approved` when no rule failed; skipped rules count as pass for the status but
are listed in their own column so a reviewer can see what was not checked.

Single-document rules: line totals, subtotal, tax, total, required fields, due date, future date.
Cross-document rules (duplicate number, date sequence) run over an "inbox" of one file per invoice,
processed in doc_id order (= arrival order). There are two inbox views: every invoice as its PDF,
and every invoice as its scan where one exists (else its PDF). PDF rows of the queue come from the
first view, scan rows from the second, so a scan is never compared against its own PDF.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract.gemini import DEFAULT_MODEL  # noqa: E402
from extract.schema import ExtractedInvoice, parse_invoice  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
QUEUE_PATH = ROOT / "data" / "review" / "queue.csv"

TOL = Decimal("0.01")
PASS, FAIL, SKIP = "pass", "fail", "skip"
AUTO_APPROVED, NEEDS_REVIEW = "auto_approved", "needs_review"
REQUIRED_FIELDS = ("invoice_number", "vendor_name", "invoice_date", "total", "currency")

QUEUE_COLUMNS = (
    "doc_key", "doc_id", "file_type", "vendor_name", "invoice_number", "invoice_date", "total",
    "currency", "status", "n_failed", "failed_reasons", "skipped_rules",
)


@dataclass(frozen=True)
class RuleResult:
    rule: str
    status: str  # pass / fail / skip
    reason: str = ""


@dataclass
class Record:
    """One extracted file: an invoice as a PDF or as a scan."""

    doc_key: str  # e.g. "doc_0071.png"
    doc_id: str
    file_type: str
    invoice: ExtractedInvoice
    extra: dict = field(default_factory=dict)  # tokens, latency, ... from the cache file


def _money(d: Decimal) -> str:
    return f"{d:.2f}"


# --------------------------------------------------------------------------------------
# Single-document rules
# --------------------------------------------------------------------------------------

def check_line_totals(inv: ExtractedInvoice) -> RuleResult:
    rule = "line_totals"
    if not inv.line_items:
        return RuleResult(rule, SKIP, "no line items extracted")
    problems = []
    for i, ln in enumerate(inv.line_items, start=1):
        missing = [f for f in ("quantity", "unit_price", "line_total") if getattr(ln, f) is None]
        if missing:
            problems.append(f"line {i}: {', '.join(missing)} missing")
            continue
        expected = ln.quantity * ln.unit_price
        if abs(expected - ln.line_total) > TOL:
            problems.append(f"line {i}: {ln.quantity} x {ln.unit_price} = {_money(expected)}, "
                            f"printed {ln.line_total}")
    return RuleResult(rule, FAIL, "; ".join(problems)) if problems else RuleResult(rule, PASS)


def check_subtotal(inv: ExtractedInvoice) -> RuleResult:
    rule = "subtotal"
    if inv.subtotal is None:
        return RuleResult(rule, SKIP, "no subtotal extracted")
    if not inv.line_items:
        return RuleResult(rule, SKIP, "no line items extracted")
    totals = [ln.line_total for ln in inv.line_items]
    if any(t is None for t in totals):
        return RuleResult(rule, SKIP, "a line total is missing")
    lines_sum = sum(totals, Decimal("0"))
    if abs(lines_sum - inv.subtotal) > TOL:
        return RuleResult(rule, FAIL, f"lines sum to {_money(lines_sum)}, subtotal printed {inv.subtotal}")
    return RuleResult(rule, PASS)


def check_tax(inv: ExtractedInvoice) -> RuleResult:
    rule = "tax"
    if inv.tax_rate is None and inv.tax_amount is None:
        return RuleResult(rule, SKIP, "no tax printed")
    if inv.tax_rate is None or inv.tax_amount is None or inv.subtotal is None:
        missing = [f for f in ("tax_rate", "tax_amount", "subtotal") if getattr(inv, f) is None]
        return RuleResult(rule, SKIP, f"{', '.join(missing)} missing")
    base = inv.subtotal - (inv.discount or 0)
    expected = base * inv.tax_rate / 100
    if abs(expected - inv.tax_amount) > TOL:
        return RuleResult(rule, FAIL, f"{inv.tax_rate.normalize():f}% of {_money(base)} = {_money(expected)}, "
                                      f"tax printed {inv.tax_amount}")
    return RuleResult(rule, PASS)


def check_total(inv: ExtractedInvoice) -> RuleResult:
    rule = "total"
    if inv.total is None or inv.subtotal is None:
        return RuleResult(rule, SKIP, "total or subtotal missing")
    if inv.tax_amount is None and inv.tax_rate is not None:
        return RuleResult(rule, SKIP, "tax rate printed but tax amount missing")
    expected = inv.subtotal - (inv.discount or 0) + (inv.tax_amount or 0)
    if abs(expected - inv.total) > TOL:
        return RuleResult(rule, FAIL, f"subtotal - discount + tax = {_money(expected)}, total printed {inv.total}")
    return RuleResult(rule, PASS)


def check_required_fields(inv: ExtractedInvoice) -> RuleResult:
    rule = "required_fields"
    missing = [f for f in REQUIRED_FIELDS if getattr(inv, f) in (None, "")]
    if missing:
        return RuleResult(rule, FAIL, f"missing: {', '.join(missing)}")
    return RuleResult(rule, PASS)


def check_due_date(inv: ExtractedInvoice) -> RuleResult:
    rule = "due_date"
    if inv.invoice_date is None or inv.due_date is None:
        return RuleResult(rule, SKIP, "invoice date or due date missing")
    if inv.due_date < inv.invoice_date:
        return RuleResult(rule, FAIL, f"due date {inv.due_date} is before invoice date {inv.invoice_date}")
    return RuleResult(rule, PASS)


def check_not_future(inv: ExtractedInvoice, today: date) -> RuleResult:
    rule = "future_date"
    if inv.invoice_date is None:
        return RuleResult(rule, SKIP, "invoice date missing")
    if inv.invoice_date > today:
        return RuleResult(rule, FAIL, f"invoice date {inv.invoice_date} is in the future (today {today})")
    return RuleResult(rule, PASS)


def validate_single(inv: ExtractedInvoice, today: date) -> list[RuleResult]:
    return [
        check_required_fields(inv),
        check_line_totals(inv),
        check_subtotal(inv),
        check_tax(inv),
        check_total(inv),
        check_due_date(inv),
        check_not_future(inv, today),
    ]


# --------------------------------------------------------------------------------------
# Cross-document rules (run over one inbox, records in arrival order)
# --------------------------------------------------------------------------------------

def vendor_key(inv: ExtractedInvoice) -> str | None:
    if not inv.vendor_name:
        return None
    return re.sub(r"\s+", " ", inv.vendor_name).strip().casefold()


_SEQ = re.compile(r"(\d+)\D*$")


def invoice_seq(number: str | None) -> int | None:
    """Running number of an invoice number: its last group of digits ('SHF2025-0380' -> 380)."""
    if not number:
        return None
    m = _SEQ.search(number)
    return int(m.group(1)) if m else None


def check_duplicates(records: list[Record]) -> dict[str, RuleResult]:
    """Flag an invoice whose vendor + invoice number was already seen earlier in the inbox.
    Only the later copy is flagged: the first one was legitimately received."""
    rule = "duplicate"
    seen: dict[tuple[str, str], Record] = {}
    out = {}
    for r in records:
        vk, num = vendor_key(r.invoice), (r.invoice.invoice_number or "").strip()
        if vk is None or not num:
            out[r.doc_key] = RuleResult(rule, SKIP, "vendor or invoice number missing")
            continue
        first = seen.get((vk, num))
        if first is not None:
            out[r.doc_key] = RuleResult(
                rule, FAIL, f"invoice number {num} from {r.invoice.vendor_name} was already received ({first.doc_id})")
        else:
            seen[(vk, num)] = r
            out[r.doc_key] = RuleResult(rule, PASS)
    return out


def check_date_sequence(records: list[Record], exclude: set[str] = frozenset()) -> dict[str, RuleResult]:
    """Per vendor, a higher invoice number should not carry an earlier date than a lower one.

    Every pair of the vendor's invoices that breaks this order is a conflict. The invoice that
    breaks the order is the one with the most conflicts: a day/month swap pushes one invoice's
    date months away, so it conflicts with many neighbours while each neighbour conflicts only
    with it. An invoice is flagged when no invoice it conflicts with has more conflicts than it
    does; when that is a tie (e.g. a single conflicting pair) both are flagged.

    `exclude`: doc_keys left out of the comparison (skipped), used for duplicates: a reused
    invoice number says nothing about where the invoice belongs in the sequence.
    """
    rule = "date_sequence"
    out: dict[str, RuleResult] = {}
    by_vendor: dict[str, list[Record]] = defaultdict(list)
    for r in records:
        inv = r.invoice
        if r.doc_key in exclude:
            out[r.doc_key] = RuleResult(rule, SKIP, "invoice number is a duplicate, not compared")
        elif vendor_key(inv) is None or inv.invoice_date is None or invoice_seq(inv.invoice_number) is None:
            out[r.doc_key] = RuleResult(rule, SKIP, "vendor, invoice number or invoice date missing")
        else:
            by_vendor[vendor_key(inv)].append(r)

    for group in by_vendor.values():
        conflicts: dict[str, list[Record]] = {r.doc_key: [] for r in group}
        for a in group:
            for b in group:
                sa, sb = invoice_seq(a.invoice.invoice_number), invoice_seq(b.invoice.invoice_number)
                if sa < sb and a.invoice.invoice_date > b.invoice.invoice_date:
                    conflicts[a.doc_key].append(b)
                    conflicts[b.doc_key].append(a)
        for r in group:
            mine = conflicts[r.doc_key]
            if len(group) == 1:
                out[r.doc_key] = RuleResult(rule, SKIP, "only invoice from this vendor so far")
            elif not mine or any(len(conflicts[o.doc_key]) > len(mine) for o in mine):
                out[r.doc_key] = RuleResult(rule, PASS)
            else:
                examples = ", ".join(
                    f"{o.invoice.invoice_number} ({o.doc_id}) dated {o.invoice.invoice_date}" for o in mine[:2])
                more = f" and {len(mine) - 2} more" if len(mine) > 2 else ""
                out[r.doc_key] = RuleResult(
                    rule, FAIL,
                    f"invoice {r.invoice.invoice_number} dated {r.invoice.invoice_date} is out of sequence with "
                    f"{examples}{more}; check for a day/month swap")
    return out


def validate_inbox(records: list[Record], today: date) -> dict[str, list[RuleResult]]:
    """Run every rule over one inbox (records in arrival order). Returns results per doc_key."""
    dups = check_duplicates(records)
    seq = check_date_sequence(records, exclude={k for k, r in dups.items() if r.status == FAIL})
    return {r.doc_key: validate_single(r.invoice, today) + [dups[r.doc_key], seq[r.doc_key]] for r in records}


# --------------------------------------------------------------------------------------
# Queue
# --------------------------------------------------------------------------------------

def queue_status(results: list[RuleResult]) -> str:
    return NEEDS_REVIEW if any(r.status == FAIL for r in results) else AUTO_APPROVED


def queue_row(record: Record, results: list[RuleResult]) -> dict:
    inv = record.invoice
    failed = [r for r in results if r.status == FAIL]
    skipped = [r for r in results if r.status == SKIP]
    return {
        "doc_key": record.doc_key,
        "doc_id": record.doc_id,
        "file_type": record.file_type,
        "vendor_name": inv.vendor_name or "",
        "invoice_number": inv.invoice_number or "",
        "invoice_date": inv.invoice_date.isoformat() if inv.invoice_date else "",
        "total": str(inv.total) if inv.total is not None else "",
        "currency": inv.currency or "",
        "status": queue_status(results),
        "n_failed": len(failed),
        "failed_reasons": "; ".join(f"{r.rule}: {r.reason}" for r in failed),
        "skipped_rules": "; ".join(f"{r.rule}: {r.reason}" for r in skipped),
    }


def load_records(model_dir: Path) -> dict[str, Record]:
    """Load every cached result, re-parsing the raw model response with the current normalisation."""
    records = {}
    for path in sorted(model_dir.glob("doc_*.json")):
        res = json.loads(path.read_text(encoding="utf-8"))
        key = path.name.removesuffix(".json")
        raw = res.get("raw_response")
        inv = parse_invoice(raw) if raw else ExtractedInvoice.model_validate(res["invoice"])
        extra = {k: res.get(k) for k in ("input_tokens", "output_tokens", "latency_s", "model", "source_file")}
        extra["cached_invoice"] = res["invoice"]  # as stored at extraction time (before later fixes)
        records[key] = Record(key, res["doc_id"], key.rsplit(".", 1)[1], inv, extra)
    return records


def inbox_views(records: dict[str, Record]) -> dict[str, list[Record]]:
    """'pdf': every invoice as its PDF. 'scan': every invoice as its scan if it has one, else its PDF."""
    by_doc: dict[str, dict[str, Record]] = defaultdict(dict)
    for r in records.values():
        by_doc[r.doc_id][r.file_type] = r
    pdf, scan = [], []
    for doc_id in sorted(by_doc):
        files = by_doc[doc_id]
        if "pdf" in files:
            pdf.append(files["pdf"])
        scans = [files[t] for t in sorted(files) if t != "pdf"]
        if scans or "pdf" in files:
            scan.append(scans[0] if scans else files["pdf"])
    return {"pdf": pdf, "scan": scan}


def build_queue(records: dict[str, Record], today: date) -> tuple[list[dict], dict[str, list[RuleResult]]]:
    """Validate every file. PDF rows come from the PDF inbox, scan rows from the scan inbox."""
    views = inbox_views(records)
    pdf_results = validate_inbox(views["pdf"], today)
    scan_results = validate_inbox(views["scan"], today)
    results: dict[str, list[RuleResult]] = {}
    for key, rec in records.items():
        results[key] = pdf_results[key] if rec.file_type == "pdf" else scan_results[key]
    rows = [queue_row(records[k], results[k]) for k in sorted(records)]
    return rows, results


def write_queue(rows: list[dict], path: Path = QUEUE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=QUEUE_COLUMNS)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def model_dir_for(model: str) -> Path:
    from run_extraction import safe_dirname

    return ROOT / "data" / "extracted" / safe_dirname(model)


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL)
    p.add_argument("--today", type=date.fromisoformat, default=date.today(),
                   help="reference date for the future-date rule (default: today)")
    p.add_argument("--out", type=Path, default=QUEUE_PATH)
    args = p.parse_args(argv)

    model_dir = model_dir_for(args.model)
    records = load_records(model_dir)
    if not records:
        print(f"No cached results in {model_dir}. Run src/run_extraction.py first.", file=sys.stderr)
        return 1
    rows, _ = build_queue(records, args.today)
    write_queue(rows, args.out)

    n_review = sum(r["status"] == NEEDS_REVIEW for r in rows)
    print(f"{len(rows)} documents validated ({args.model}, today={args.today})")
    print(f"  auto_approved: {len(rows) - n_review}")
    print(f"  needs_review:  {n_review}")
    for r in rows:
        if r["status"] == NEEDS_REVIEW:
            print(f"    {r['doc_key']}: {r['failed_reasons']}")
    print(f"Queue written to {args.out.relative_to(ROOT) if args.out.is_relative_to(ROOT) else args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
