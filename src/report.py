"""Build docs/accuracy_report.md from cached extraction results and ground truth (no API calls).

    python src/report.py                                   # LLM_MODEL or the default model
    python src/report.py --model gemini-3.5-flash-lite --today 2026-11-08

Field accuracy is reported twice: on the output as cached at extraction time ("before fix") and on
the same model output re-normalised with the current pipeline rules ("after fix", e.g. discounts
stored as positive amounts). The model is not re-run for "after fix".
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import sys
from collections import defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from compare_samples import compare_doc  # noqa: E402
from extract.gemini import DEFAULT_MODEL  # noqa: E402
from extract.schema import ExtractedInvoice  # noqa: E402
from validate import (  # noqa: E402
    FAIL, NEEDS_REVIEW, Record, build_queue, load_records, model_dir_for, queue_status,
)

ROOT = Path(__file__).resolve().parents[1]
GT_DIR = ROOT / "data" / "synthetic" / "ground_truth"
REPORT_PATH = ROOT / "docs" / "accuracy_report.md"

# Official paid-tier (Standard) prices, USD per 1M tokens. Update together with the date.
PRICING = {
    "gemini-3.5-flash-lite": {"input": Decimal("0.30"), "output": Decimal("2.50"),
                              "batch_input": Decimal("0.15"), "batch_output": Decimal("1.25")},
}
PRICING_SOURCE = "https://ai.google.dev/gemini-api/docs/pricing"
PRICING_CHECKED = "2026-11-08"

TEMPLATE_NAMES = {
    "T1_classic": "T1 classic",
    "T2_modern": "T2 modern",
    "T3_receipt": "T3 receipt",
    "T4_two_column": "T4 two-column",
    "T5_bilingual_ar_en": "T5 bilingual AR/EN",
}


# --------------------------------------------------------------------------------------
# Field accuracy
# --------------------------------------------------------------------------------------

def field_group(name: str) -> str:
    """'line_items[3].unit_price' -> 'line_items.unit_price'; header fields unchanged."""
    if name.startswith("line_items["):
        return "line_items." + name.split("].", 1)[1]
    return name


def invoice_dict(inv: ExtractedInvoice) -> dict:
    return json.loads(inv.model_dump_json())


class Tally:
    def __init__(self) -> None:
        self.ok = 0
        self.n = 0

    def add(self, ok: bool) -> None:
        self.ok += ok
        self.n += 1

    def pct(self) -> str:
        """Rounded down to 0.1%, so 4990/4992 reads 99.9% and only a perfect score reads 100%."""
        if not self.n:
            return "n/a"
        if self.ok == self.n:
            return "100%"
        return f"{math.floor(self.ok * 1000 / self.n) / 10:.1f}%"

    def cell(self) -> str:
        return f"{self.pct()} ({self.ok}/{self.n})" if self.n else "n/a"


def accuracy(records: dict[str, Record], gts: dict[str, dict], version: str) -> dict:
    """version 'before' = cached invoice as stored; 'after' = re-normalised from raw response."""
    by_field: dict[str, Tally] = defaultdict(Tally)
    by_group: dict[tuple[str, str], Tally] = defaultdict(Tally)
    overall = Tally()
    docs_all_correct = Tally()
    wrong: dict[str, list] = {}
    for key, rec in records.items():
        gt = gts[rec.doc_id]
        got = rec.extra["cached_invoice"] if version == "before" else invoice_dict(rec.invoice)
        rows = compare_doc(gt["invoice"], got)
        meta = gt["meta"]
        groups = [
            ("template", TEMPLATE_NAMES.get(meta["template"], meta["template"])),
            ("file_type", "PDF" if rec.file_type == "pdf" else "Scan (PNG)"),
            ("digits", "Arabic-Indic" if meta["digits"] == "arabic_indic" else "Western"),
        ]
        if meta["template"] == "T5_bilingual_ar_en":
            groups.append(("digits_t5", "Arabic-Indic" if meta["digits"] == "arabic_indic" else "Western"))
        bad = [r for r in rows if r[1] != "match"]
        if bad:
            wrong[key] = bad
        docs_all_correct.add(not bad)
        for name, status, _, _ in rows:
            ok = status == "match"
            overall.add(ok)
            by_field[field_group(name)].add(ok)
            for g in groups:
                by_group[g].add(ok)
    return {"overall": overall, "by_field": by_field, "by_group": by_group,
            "docs": docs_all_correct, "wrong": wrong}


# --------------------------------------------------------------------------------------
# Cost / latency
# --------------------------------------------------------------------------------------

def p95(values: list[float]) -> float:
    """Nearest-rank 95th percentile."""
    s = sorted(values)
    return s[max(0, math.ceil(0.95 * len(s)) - 1)]


def mean(values) -> float:
    values = list(values)
    return sum(values) / len(values) if values else 0.0


# --------------------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------------------

def _table(header: list[str], rows: list[list]) -> str:
    out = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    out += ["| " + " | ".join(str(c) for c in row) + " |" for row in rows]
    return "\n".join(out)


def build_report(model: str, today: date) -> str:
    records = load_records(model_dir_for(model))
    if not records:
        raise SystemExit(f"No cached results for {model}. Run src/run_extraction.py first.")
    gts = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in GT_DIR.glob("doc_*.json")}
    n_files = len(records)
    n_pdf = sum(r.file_type == "pdf" for r in records.values())
    n_scan = n_files - n_pdf

    before = accuracy(records, gts, "before")
    after = accuracy(records, gts, "after")

    # Review queue on the current pipeline, and on the un-fixed output for the extraction-error table.
    rows, results = build_queue(records, today)
    unfixed = {k: Record(r.doc_key, r.doc_id, r.file_type,
                         ExtractedInvoice.model_validate(r.extra["cached_invoice"]), r.extra)
               for k, r in records.items()}
    _, results_unfixed = build_queue(unfixed, today)
    status = {r["doc_key"]: r["status"] for r in rows}
    n_review = sum(s == NEEDS_REVIEW for s in status.values())

    def is_anomaly(rec: Record) -> bool:
        return bool(gts[rec.doc_id]["is_anomaly"])

    anomalies = {k: r for k, r in records.items() if is_anomaly(r)}
    caught = {k for k in anomalies if status[k] == NEEDS_REVIEW}
    anomaly_docs = sorted({r.doc_id for r in anomalies.values()})
    anomaly_docs_caught = sorted({anomalies[k].doc_id for k in caught if anomalies[k].file_type == "pdf"})
    clean = {k: r for k, r in records.items() if not is_anomaly(r) and k not in after["wrong"]}
    false_alarms = sorted(k for k in clean if status[k] == NEEDS_REVIEW)

    # Extraction errors = model output that differs from ground truth before any pipeline fix.
    errors = sorted(before["wrong"])

    # Cost
    price = PRICING.get(model)
    tin = {t: mean(r.extra["input_tokens"] or 0 for r in records.values() if t in ("all", r.file_type))
           for t in ("all", "pdf", "png")}
    tout = {t: mean(r.extra["output_tokens"] or 0 for r in records.values() if t in ("all", r.file_type))
            for t in ("all", "pdf", "png")}
    lat = {t: [r.extra["latency_s"] for r in records.values() if t in ("all", r.file_type)]
           for t in ("all", "pdf", "png")}

    def cost_per_1000(t: str, batch: bool = False) -> Decimal:
        pi = price["batch_input" if batch else "input"]
        po = price["batch_output" if batch else "output"]
        per_doc = (Decimal(str(tin[t])) * pi + Decimal(str(tout[t])) * po) / Decimal(1_000_000)
        return per_doc * 1000

    # ------------------------------------------------------------------ text
    L: list[str] = []
    a = L.append
    a("# Accuracy report: invoice extraction")
    a("")
    a(f"Model `{model}` · {n_files} files ({n_pdf} PDFs + {n_scan} scans of the same invoices) · "
      f"synthetic dataset, seed 42 · rules evaluated as of {today}.")
    a("Generated by `src/report.py` from cached results; regenerating it makes no API calls.")
    a("")
    a("## Summary")
    a("")
    pdf_rule = (f"all {len(anomaly_docs)}" if len(anomaly_docs_caught) == len(anomaly_docs)
                else f"{len(anomaly_docs_caught)} of {len(anomaly_docs)}")
    if price:
        cost_line = (f"- **Cost:** about ${cost_per_1000('all'):.2f} per 1,000 invoices on the paid tier, "
                     f"about {mean(lat['all']):.1f} s per invoice.")
    else:
        cost_line = f"- **Speed:** about {mean(lat['all']):.1f} s per invoice (no price on file for this model)."
    L += [
        f"- **Accuracy:** {after['overall'].pct()} of all extracted fields match the source exactly "
        f"({after['overall'].n - after['overall'].ok} wrong out of {after['overall'].n:,}); "
        f"{after['docs'].ok} of {n_files} documents were read with every field correct.",
        f"- **Problem invoices:** the checks sent {pdf_rule} deliberately faulty invoices "
        f"(wrong totals, wrong tax, missing dates, duplicate numbers) to a person for review.",
        f"- **False alarms:** {len(false_alarms)} correct invoices were sent to review; "
        f"{status_count(status, 'auto_approved')} of {n_files} documents passed every check and were approved automatically.",
        misses_line(errors, status),
        cost_line,
    ]
    a("")
    a("Results are on clean, computer-generated invoices; real-world accuracy will be lower (see Limitations).")
    a("")

    # ---- accuracy
    a("## 1. Field-level accuracy")
    a("")
    a("Each extracted value is compared with the ground truth: amounts by numeric value, dates as ISO dates, "
      "text by exact match after trimming (a case-only difference counts as wrong). Line items are compared by "
      "position. A field that is correctly empty (e.g. no discount printed, model returned none) counts as correct.")
    a("")
    a("**Before fix** is the model output as stored at extraction time. **After fix** is the same model output "
      "after one extra pipeline normalisation rule: *discounts are always stored as a positive amount* "
      "(`src/extract/normalize.py`). This is a rule applied to the cached responses, **not a re-run of the model**: "
      "no API calls were made and the model's raw text is unchanged.")
    a("")
    a(_table(["", "Before fix", "After fix"], [
        ["All fields", before["overall"].cell(), after["overall"].cell()],
        ["Documents with every field correct", before["docs"].cell(), after["docs"].cell()],
    ]))
    a("")
    a("### Per field")
    a("")
    fields = list(after["by_field"])
    a(_table(["Field", "Before fix", "After fix"],
             [[f"`{f}`", before["by_field"][f].cell(), after["by_field"][f].cell()] for f in fields]))
    a("")

    def group_table(title: str, dim: str, note: str = "") -> None:
        a(f"### {title}")
        a("")
        if note:
            a(note)
            a("")
        keys = sorted(k for k in after["by_group"] if k[0] == dim)
        a(_table(["", "Before fix", "After fix"],
                 [[k[1], before["by_group"][k].cell(), after["by_group"][k].cell()] for k in keys]))
        a("")

    group_table("Per template", "template")
    group_table("PDF vs scan", "file_type",
                f"The {n_scan} scans are degraded images (blur, noise, rotation, JPEG artefacts) of {n_scan} of the "
                f"{n_pdf} PDFs.")
    group_table("Arabic-Indic vs western digits (all documents)", "digits",
                "Only the bilingual template prints Arabic-Indic digits, so this mixes in template effects.")
    group_table("Arabic-Indic vs western digits (bilingual template only)", "digits_t5",
                "Same template on both rows: the fairer comparison.")

    a("### Every field the model got wrong")
    a("")
    err_rows = []
    for k in errors:
        fixed = k not in after["wrong"]
        for name, _, exp, got in before["wrong"][k]:
            err_rows.append([f"`{k}`", f"`{name}`", exp, got,
                             "fixed by normalisation" if fixed else "still wrong"])
    a(_table(["File", "Field", "Correct", "Model read", "After fix"], err_rows) if err_rows else "None.")
    a("")

    # ---- review queue
    a("## 2. Review queue")
    a("")
    a("Every document runs through these checks (`src/validate.py`). Any failure sends it to "
      "`needs_review` with the reasons; otherwise it is `auto_approved`. A check that cannot run (e.g. no tax "
      "printed) is *skipped*: it does not block approval but is listed in the queue's `skipped_rules` column.")
    a("")
    a(_table(["Check", "What it catches"], [
        ["line_totals", "quantity x unit price != line total"],
        ["subtotal", "line totals do not add up to the subtotal"],
        ["tax", "(subtotal - discount) x rate != tax amount"],
        ["total", "subtotal - discount + tax != total"],
        ["required_fields", "invoice number, vendor, invoice date, total or currency missing"],
        ["due_date", "due date before invoice date"],
        ["future_date", "invoice date after today"],
        ["duplicate", "same vendor + invoice number already received"],
        ["date_sequence", "a higher invoice number with an earlier date than a lower one (same vendor); "
                          "catches day/month swaps that move a date past its neighbours"],
    ]))
    a("")
    a("All arithmetic checks allow 0.01 for rounding. Duplicate and sequence checks compare each document with "
      "the other invoices in the same batch, in arrival order (doc_id). Scans are checked in a batch where those "
      "invoices arrive as scans, so a scan is never compared with its own PDF.")
    a("")
    a(_table(["Queue status", "PDFs", "Scans", "All"], [
        ["auto_approved", *counts(rows, "auto_approved")],
        ["needs_review", *counts(rows, NEEDS_REVIEW)],
    ]))
    a("")
    a("### Deliberate anomalies (recall)")
    a("")
    a(f"The dataset has {len(anomaly_docs)} invoices with a deliberate problem printed on the document. "
      f"Some also have a scan, so there are {len(anomalies)} anomalous files.")
    a("")
    by_type: dict[str, dict] = defaultdict(lambda: {"pdf": [0, 0], "png": [0, 0]})
    for k, r in anomalies.items():
        t = gts[r.doc_id]["anomaly"]["type"]
        by_type[t][r.file_type][1] += 1
        by_type[t][r.file_type][0] += k in caught
    tr = []
    for t in sorted(by_type):
        p, s = by_type[t]["pdf"], by_type[t]["png"]
        tr.append([t, f"{p[0]}/{p[1]}", f"{s[0]}/{s[1]}" if s[1] else "-"])
    cp = sum(v["pdf"][0] for v in by_type.values()), sum(v["pdf"][1] for v in by_type.values())
    cs = sum(v["png"][0] for v in by_type.values()), sum(v["png"][1] for v in by_type.values())
    tr.append(["**All**", f"**{cp[0]}/{cp[1]}**", f"**{cs[0]}/{cs[1]}**"])
    a(_table(["Anomaly type", "PDFs sent to review", "Scans sent to review"], tr))
    a("")
    missed = sorted(set(anomalies) - caught)
    if missed:
        a("Missed: " + ", ".join(f"`{k}`" for k in missed) + ".")
        a("")
    a("### False alarms")
    a("")
    a(f"{len(clean)} files are clean invoices that the model read with every field correct. "
      f"**{len(false_alarms)}** of them were sent to review"
      + (": " + ", ".join(f"`{k}`" for k in false_alarms) + "." if false_alarms else "."))
    a("")
    a("### Extraction errors caught by the checks")
    a("")
    a(f"The model misread {len(errors)} of {n_files} files (table in section 1). For each one: was it sent "
      "to review, and by which check? *Unfixed* is the stored model output, *current* is the pipeline today.")
    a("")
    er = []
    for k in errors:
        rec = records[k]
        fails_now = [r.rule for r in results[k] if r.status == FAIL]
        fails_raw = [r.rule for r in results_unfixed[k] if r.status == FAIL]
        note = []
        if is_anomaly(rec):
            note.append(f"also a deliberate anomaly ({gts[rec.doc_id]['anomaly']['type']})")
        er.append([f"`{k}`", ", ".join(f"`{f[0]}`" for f in before["wrong"][k]),
                   ", ".join(fails_raw) or "**not caught**",
                   ", ".join(fails_now) or "**not caught**",
                   "; ".join(note)])
    a(_table(["File", "Wrong fields", "Checks failed (unfixed)", "Checks failed (current)", "Note"], er))
    a("")
    caught_errors = [k for k in errors if queue_status(results_unfixed[k]) == NEEDS_REVIEW]
    a(f"Caught on the unfixed output: **{len(caught_errors)} of {len(errors)}**.")
    a("")
    uncaught = [k for k in errors if queue_status(results[k]) != NEEDS_REVIEW]
    for k in uncaught:
        a(uncaught_note(k, records, gts))
        a("")

    # ---- cost
    a("## 3. Cost")
    a("")
    a("Token counts are the ones the API reported for each document (prompt + document image/PDF in; "
      "JSON out; this model used no thinking tokens).")
    a("")
    a(_table(["", "Files", "Avg input tokens", "Avg output tokens"], [
        ["PDF", n_pdf, f"{tin['pdf']:,.0f}", f"{tout['pdf']:,.0f}"],
        ["Scan", n_scan, f"{tin['png']:,.0f}", f"{tout['png']:,.0f}"],
        ["All", n_files, f"{tin['all']:,.0f}", f"{tout['all']:,.0f}"],
    ]))
    a("")
    if price:
        a(f"**Prices** (paid tier, Standard, `{model}`): ${price['input']} per 1M input tokens, "
          f"${price['output']} per 1M output tokens (output includes thinking tokens). "
          f"Batch API: ${price['batch_input']} / ${price['batch_output']}. "
          f"Source: [Gemini API pricing]({PRICING_SOURCE}), checked {PRICING_CHECKED}.")
        a("")
        a("**Math** (all files):")
        a("")
        ci = Decimal(str(tin["all"])) * 1000 * price["input"] / Decimal(1_000_000)
        co = Decimal(str(tout["all"])) * 1000 * price["output"] / Decimal(1_000_000)
        a("```")
        a(f"input : {tin['all']:,.0f} tokens x 1,000 invoices = {tin['all'] * 1000:,.0f} tokens"
          f" x ${price['input']} / 1M = ${ci:.4f}")
        a(f"output: {tout['all']:,.0f} tokens x 1,000 invoices = {tout['all'] * 1000:,.0f} tokens"
          f" x ${price['output']} / 1M = ${co:.4f}")
        a(f"total : ${ci + co:.4f} per 1,000 invoices  (${(ci + co) / 1000:.6f} per invoice)")
        a("```")
        a("")
        a(_table(["Per 1,000 invoices", "Standard", "Batch API"], [
            ["PDFs", f"${cost_per_1000('pdf'):.2f}", f"${cost_per_1000('pdf', True):.2f}"],
            ["Scans", f"${cost_per_1000('png'):.2f}", f"${cost_per_1000('png', True):.2f}"],
            ["Mix in this dataset", f"${cost_per_1000('all'):.2f}", f"${cost_per_1000('all', True):.2f}"],
        ]))
        a("")
        retried = retried_docs(model_dir_for(model) / "runs.csv")
        a(f"Excludes retries ({'none were needed in this run' if not retried else f'{retried} documents needed more than one attempt'}), "
          "storage and the cost of the human review itself. The run was made with a free-tier AI Studio key "
          "(see README); the figures above are what the same tokens cost on the paid tier.")
    else:
        a(f"No price on file for `{model}`; add it to `PRICING` in `src/report.py`.")
    a("")

    # ---- latency
    a("## 4. Latency")
    a("")
    a("Wall-clock time of the successful API call per document, as recorded at extraction time "
      "(documents sent one at a time; excludes waits between retries).")
    a("")
    a(_table(["", "Files", "Average", "p95"], [
        ["PDF", len(lat["pdf"]), f"{mean(lat['pdf']):.2f} s", f"{p95(lat['pdf']):.2f} s"],
        ["Scan", len(lat["png"]), f"{mean(lat['png']):.2f} s", f"{p95(lat['png']):.2f} s"],
        ["All", len(lat["all"]), f"{mean(lat['all']):.2f} s", f"{p95(lat['all']):.2f} s"],
    ]))
    a("")
    a("p95 = the time 95% of documents finished within (nearest-rank).")
    a("")

    # ---- limitations
    a("## 5. Limitations")
    a("")
    L += [
        "- **Synthetic data.** All invoices were generated by `src/generate_invoices.py`: 5 clean layouts, "
        "invented vendors, consistent formatting. Real invoices vary far more (handwriting, stamps, multi-page "
        "tables, photos taken on phones, unusual layouts), so **real-world accuracy will be lower** than the numbers "
        "here. That is exactly why every document goes through the checks and the review queue.",
        "- **Small sample.** 120 invoices / 156 files. One extra error moves document-level accuracy by about "
        "0.6 points; treat the percentages as indicative.",
        "- **Exact match only.** A field that is close but not identical counts as wrong; we do not grade partial "
        "correctness. Correctly-empty fields count as correct, which lifts the overall percentage; the per-field "
        "table shows where that matters (e.g. `discount`).",
        "- **The checks cannot see everything.** They catch inconsistencies (sums, dates, duplicates), not a value "
        "that is wrong but still consistent. A misread date that stays in plausible order, or a misread amount on "
        "a line that also misreads its total consistently, passes. The anomalies were designed by us, so recall "
        "on real-world problems is untested.",
        "- **Batch-dependent checks.** Duplicate and date-sequence checks only see invoices in the same batch; "
        "in production they would query all previously processed invoices.",
        "- **Prices and latency change.** Prices are from the official page on the date above; latency was measured "
        "on one run from one location on the free tier.",
    ]
    a("")
    a("## Regenerate")
    a("")
    a("```bash")
    a(".venv/bin/python src/validate.py   # writes data/review/queue.csv")
    a(".venv/bin/python src/report.py     # writes docs/accuracy_report.md")
    a("```")
    a("")
    return "\n".join(L)


def misses_line(errors: list[str], status: dict[str, str]) -> str:
    missed = [k for k in errors if status[k] != NEEDS_REVIEW]
    if not missed:
        return f"- **Misses:** all {len(errors)} reading errors the model made were caught or fixed by the pipeline."
    return (f"- **Misses:** the model misread {len(errors)} of {len(status)} documents; {len(missed)} of those "
            f"got through the checks ({', '.join(missed)}: a value that was wrong but still looked consistent; "
            "see section 2).")


def retried_docs(runs_csv: Path) -> int:
    """Documents whose successful extraction took more than one attempt (from runs.csv)."""
    if not runs_csv.exists():
        return 0
    with runs_csv.open(encoding="utf-8") as fh:
        return len({r["doc_key"] for r in csv.DictReader(fh)
                    if r.get("status") == "ok" and int(r.get("attempts") or 1) > 1})


def status_count(status: dict[str, str], value: str) -> int:
    return sum(s == value for s in status.values())


def counts(rows: list[dict], value: str) -> list[int]:
    pdf = sum(r["status"] == value and r["file_type"] == "pdf" for r in rows)
    scan = sum(r["status"] == value and r["file_type"] != "pdf" for r in rows)
    return [pdf, scan, pdf + scan]


def uncaught_note(key: str, records: dict[str, Record], gts: dict[str, dict]) -> str:
    rec = records[key]
    inv = rec.invoice
    vk = (inv.vendor_name or "").casefold()
    same = sorted(
        (r for r in records.values()
         if r.file_type == "pdf" and r.doc_id != rec.doc_id and (r.invoice.vendor_name or "").casefold() == vk
         and r.invoice.invoice_date),
        key=lambda r: r.invoice.invoice_date)
    before = [r for r in same if r.invoice.invoice_date <= inv.invoice_date] if inv.invoice_date else []
    after = [r for r in same if inv.invoice_date and r.invoice.invoice_date > inv.invoice_date]
    gt = gts[rec.doc_id]["invoice"]
    ctx = ""
    if before and after:
        ctx = (f" The misread date {inv.invoice_date} still falls between the vendor's neighbouring invoices "
               f"({before[-1].invoice.invoice_number} on {before[-1].invoice.invoice_date} and "
               f"{after[0].invoice.invoice_number} on {after[0].invoice.invoice_date}), and the due date was "
               f"misread the same way, so every date check is consistent.")
    return (f"**Why `{key}` was not caught:** the correct invoice date is {gt['invoice_date']}; "
            f"the model read {inv.invoice_date}.{ctx} No rule can detect this from the extracted data alone. "
            "Possible next steps: have the model also return dates exactly as printed and flag ambiguous ones "
            "(day <= 12 in a DD/MM or MM/DD layout), or check dates against each vendor's known date format.")


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL)
    p.add_argument("--today", type=date.fromisoformat, default=date.today(),
                   help="reference date for the future-date rule (default: today)")
    p.add_argument("--out", type=Path, default=REPORT_PATH)
    args = p.parse_args(argv)
    text = build_report(args.model, args.today)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Report written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
