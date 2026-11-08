"""Print a field-by-field comparison of cached extractions against ground truth (console only).

    python src/compare_samples.py                         # data/samples vs data/extracted/<LLM_MODEL>
    python src/compare_samples.py --model gemini-3.8-flash

Rules: decimals compare by numeric value, dates by ISO value, text by exact match after trimming.
Text that matches only after case-folding and collapsing whitespace is shown as a NEAR match
and still counts as a mismatch. Line items are compared by position.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract.gemini import DEFAULT_MODEL  # noqa: E402
from extract.schema import DATE_FIELDS, HEADER_FIELDS, LINE_NUMBER_FIELDS, MONEY_FIELDS  # noqa: E402
from run_extraction import ROOT, safe_dirname  # noqa: E402

NUMERIC = set(MONEY_FIELDS) | set(LINE_NUMBER_FIELDS)


def _dec(v):
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _fold(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().casefold()


def compare_value(field: str, expected, got) -> str:
    """Return 'match', 'near' or 'diff'."""
    if expected is None or got is None:
        return "match" if expected is None and got is None else "diff"
    if field in NUMERIC:
        e, g = _dec(expected), _dec(got)
        return "match" if e is not None and e == g else "diff"
    if field in DATE_FIELDS:
        return "match" if str(expected) == str(got) else "diff"
    e, g = str(expected).strip(), str(got).strip()
    if e == g:
        return "match"
    return "near" if _fold(e) == _fold(g) else "diff"


def compare_doc(gt: dict, got: dict) -> list[tuple[str, str, object, object]]:
    rows = [(f, compare_value(f, gt.get(f), got.get(f)), gt.get(f), got.get(f)) for f in HEADER_FIELDS]
    gl, xl = gt.get("line_items") or [], got.get("line_items") or []
    rows.append(("line_items.count", "match" if len(gl) == len(xl) else "diff", len(gl), len(xl)))
    for i in range(max(len(gl), len(xl))):
        e = gl[i] if i < len(gl) else {}
        g = xl[i] if i < len(xl) else {}
        for f in ("description", *LINE_NUMBER_FIELDS):
            rows.append((f"line_items[{i}].{f}", compare_value(f, e.get(f), g.get(f)), e.get(f), g.get(f)))
    return rows


def main() -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser()
    p.add_argument("--input", type=Path, default=ROOT / "data" / "samples")
    p.add_argument("--model", default=os.environ.get("LLM_MODEL", "").strip() or DEFAULT_MODEL)
    args = p.parse_args()
    out_dir = ROOT / "data" / "extracted" / safe_dirname(args.model)

    tot = {"match": 0, "near": 0, "diff": 0}
    tokens_in = tokens_out = 0
    latencies = []
    for gt_path in sorted(args.input.glob("doc_*.json")):
        gt_doc = json.loads(gt_path.read_text(encoding="utf-8"))
        res_path = out_dir / gt_path.name
        meta = gt_doc["meta"]
        res = json.loads(res_path.read_text(encoding="utf-8")) if res_path.exists() else None
        kind = Path(res["source_file"]).suffix.lstrip(".") if res else "?"
        head = f"{gt_doc['doc_id']}  [{meta['template']}, {kind}, {meta['digits']} digits]"
        if gt_doc.get("is_anomaly"):
            head += f"  anomaly: {gt_doc['anomaly']['type']}"
        print("=" * 100 + f"\n{head}")
        if res is None:
            print("  no extraction result")
            continue
        tokens_in += res.get("input_tokens") or 0
        tokens_out += res.get("output_tokens") or 0
        latencies.append(res["latency_s"])
        rows = compare_doc(gt_doc["invoice"], res["invoice"])
        counts = {k: sum(1 for r in rows if r[1] == k) for k in tot}
        for k in tot:
            tot[k] += counts[k]
        print(f"  {counts['match']}/{len(rows)} exact  |  {counts['near']} near  |  {counts['diff']} differ"
              f"  |  {res['latency_s']:.1f}s, {res.get('input_tokens')} in / {res.get('output_tokens')} out")
        matched = [r[0] for r in rows if r[1] == "match"]
        print(f"  exact: {', '.join(matched)}")
        for f, status, e, g in rows:
            if status != "match":
                print(f"  {'NEAR' if status == 'near' else 'DIFF'} {f}: expected {e!r}  got {g!r}")
        unc = res["invoice"].get("uncertain_fields") or []
        if unc:
            print(f"  model flagged uncertain: {', '.join(unc)}")

    n = sum(tot.values())
    print("=" * 100)
    if n:
        print(f"All docs: {tot['match']}/{n} fields exact ({tot['match'] / n:.1%}), "
              f"{tot['near']} near (counted as mismatch), {tot['diff']} differ")
    if latencies:
        print(f"Tokens: {tokens_in} in + {tokens_out} out = {tokens_in + tokens_out} total; "
              f"avg latency {sum(latencies) / len(latencies):.2f}s over {len(latencies)} docs")
    return 0


if __name__ == "__main__":
    sys.exit(main())
