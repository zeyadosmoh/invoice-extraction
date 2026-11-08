"""Run LLM extraction over a folder of invoices (PDF / PNG) with caching, throttling and backoff.

    python src/run_extraction.py                      # data/samples, cached results are skipped
    python src/run_extraction.py --force              # re-run everything
    python src/run_extraction.py --input data/synthetic/pdf --delay 15

Outputs (gitignored):
    data/extracted/<model>/<doc_id>.json      one result per document (the cache)
    data/extracted/<model>/<doc_id>.raw.txt   raw response, only when it failed to parse
    data/extracted/<model>/runs.csv           one row per document per run

Exit codes: 0 ok, 1 some documents failed, 2 stopped on quota / rate limit, 3 configuration error.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract import get_extractor  # noqa: E402
from extract.base import (  # noqa: E402
    MIME_TYPES,
    ConfigError,
    Extractor,
    QuotaExhaustedError,
    RateLimitError,
    ResponseParseError,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "samples"
DEFAULT_OUT = ROOT / "data" / "extracted"
GT_DIR = ROOT / "data" / "synthetic" / "ground_truth"
RUNS_COLUMNS = [
    "run_at", "doc_id", "file_type", "template", "model", "prompt_version",
    "latency_s", "input_tokens", "output_tokens", "attempts", "status", "error",
]


@dataclass
class RunSummary:
    ok: list[str] = field(default_factory=list)
    cached: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    stopped_reason: str | None = None

    @property
    def exit_code(self) -> int:
        if self.stopped_reason:
            return 2
        return 1 if self.failed else 0


def safe_dirname(model: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", model).strip("_") or "model"


def find_documents(input_dir: Path) -> list[Path]:
    docs = sorted(p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in MIME_TYPES)
    stems = [p.stem for p in docs]
    dupes = {s for s in stems if stems.count(s) > 1}
    if dupes:
        raise ConfigError(f"several files share a doc_id in {input_dir}: {sorted(dupes)}")
    return docs


def template_for(doc: Path) -> str:
    """Look up the template from ground truth next to the doc, else the synthetic ground truth."""
    for gt in (doc.with_suffix(".json"), GT_DIR / f"{doc.stem}.json"):
        if gt.is_file():
            try:
                return json.loads(gt.read_text(encoding="utf-8"))["meta"]["template"]
            except (KeyError, ValueError):
                pass
    return ""


def _append_row(csv_path: Path, row: dict) -> None:
    new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in RUNS_COLUMNS})


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def backoff_delay(attempt: int, base: float, cap: float, retry_after: float | None, rng: random.Random) -> float:
    """Exponential backoff with full jitter, never shorter than the server's retry hint."""
    d = min(cap, base * 2 ** attempt) * (0.5 + rng.random() / 2)
    return max(d, retry_after or 0.0)


def run(
    extractor: Extractor,
    input_dir: Path,
    out_root: Path = DEFAULT_OUT,
    *,
    force: bool = False,
    delay_s: float = 10.0,
    max_retries: int = 5,
    backoff_base_s: float = 10.0,
    backoff_cap_s: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> RunSummary:
    out_dir = out_root / safe_dirname(extractor.model)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "runs.csv"
    rng = random.Random()
    summary = RunSummary()
    docs = find_documents(input_dir)
    log(f"{len(docs)} document(s) in {input_dir} -> {out_dir} (model {extractor.model})")

    made_request = False
    for doc in docs:
        doc_id = doc.stem
        cache = out_dir / f"{doc_id}.json"
        base_row = {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "doc_id": doc_id,
            "file_type": doc.suffix.lower().lstrip("."),
            "template": template_for(doc),
            "model": extractor.model,
        }
        if cache.exists() and not force:
            summary.cached.append(doc_id)
            _append_row(csv_path, {**base_row, "status": "cached"})
            log(f"  {doc_id}: cached, skipping (use --force to re-run)")
            continue

        attempt = 0
        while True:
            if made_request:
                sleep(delay_s)
            made_request = True
            attempt += 1
            try:
                result = extractor.extract(doc)
            except RateLimitError as e:
                if attempt > max_retries:
                    msg = f"rate limited after {attempt} attempts: {e}"
                    _append_row(csv_path, {**base_row, "attempts": attempt, "status": "rate_limited", "error": msg})
                    summary.stopped_reason = (
                        f"Still rate limited on {doc_id} after {max_retries} retries. This usually means the "
                        "free-tier daily quota is used up (it resets at midnight Pacific time). "
                        "Re-run later; finished documents are cached."
                    )
                    log(f"  {doc_id}: {msg}")
                    return summary
                wait = backoff_delay(attempt - 1, backoff_base_s, backoff_cap_s, e.retry_after_s, rng)
                log(f"  {doc_id}: rate limited (attempt {attempt}), retrying in {wait:.0f}s")
                sleep(wait)
                continue
            except QuotaExhaustedError as e:
                _append_row(csv_path, {**base_row, "attempts": attempt, "status": "quota_exhausted", "error": str(e)})
                summary.stopped_reason = (
                    f"Daily quota exhausted at {doc_id}. It resets at midnight Pacific time. "
                    "Re-run later; finished documents are cached and will be skipped."
                )
                log(f"  {doc_id}: quota exhausted")
                return summary
            except ResponseParseError as e:
                (out_dir / f"{doc_id}.raw.txt").write_text(e.raw_response, encoding="utf-8")
                _append_row(csv_path, {**base_row, "attempts": attempt, "status": "parse_error", "error": str(e)})
                summary.failed.append(doc_id)
                log(f"  {doc_id}: parse error: {e}")
                break
            except NotImplementedError as e:
                raise ConfigError(str(e)) from None
            except Exception as e:  # extractor errors are already redacted; keep going with the next doc
                _append_row(csv_path, {**base_row, "attempts": attempt, "status": "error", "error": str(e)})
                summary.failed.append(doc_id)
                log(f"  {doc_id}: error: {e}")
                break

            _write_json_atomic(cache, result.to_json_dict())
            _append_row(csv_path, {
                **base_row,
                "prompt_version": result.prompt_version,
                "latency_s": f"{result.latency_s:.3f}",
                "input_tokens": result.input_tokens,
                "output_tokens": result.output_tokens,
                "attempts": attempt,
                "status": "ok",
            })
            summary.ok.append(doc_id)
            log(f"  {doc_id}: ok in {result.latency_s:.1f}s ({result.input_tokens} in / {result.output_tokens} out)")
            break
    return summary


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="folder with PDFs / PNGs (default data/samples)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output root (default data/extracted)")
    p.add_argument("--force", action="store_true", help="re-run documents that already have a cached result")
    p.add_argument("--delay", type=float, default=float(os.environ.get("LLM_REQUEST_DELAY_S", "10")),
                   help="seconds between API requests (default LLM_REQUEST_DELAY_S or 10)")
    p.add_argument("--max-retries", type=int, default=5, help="retries per document on rate-limit errors")
    args = p.parse_args(argv)

    if not args.input.is_dir():
        print(f"Input folder not found: {args.input}", file=sys.stderr)
        return 3
    try:
        extractor = get_extractor()
        summary = run(extractor, args.input, args.out, force=args.force,
                      delay_s=args.delay, max_retries=args.max_retries)
    except ConfigError as e:
        print(f"Configuration error: {e}", file=sys.stderr)
        return 3

    print(f"\nDone: {len(summary.ok)} extracted, {len(summary.cached)} cached, {len(summary.failed)} failed.")
    if summary.failed:
        print(f"Failed: {', '.join(summary.failed)} (see runs.csv)")
    if summary.stopped_reason:
        print(f"STOPPED: {summary.stopped_reason}", file=sys.stderr)
    return summary.exit_code


if __name__ == "__main__":
    sys.exit(main())
