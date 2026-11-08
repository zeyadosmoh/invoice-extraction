"""Run LLM extraction over a folder of invoices (PDF / PNG) with caching, throttling and backoff.

    python src/run_extraction.py                      # data/samples, cached results are skipped
    python src/run_extraction.py --force              # re-run everything
    python src/run_extraction.py --input data/synthetic/pdf --delay 15

Outputs (gitignored):
    data/extracted/<model>/<doc_id>.<ext>.json      one result per document file (the cache)
    data/extracted/<model>/<doc_id>.<ext>.raw.txt   raw response, only when it failed to parse
    data/extracted/<model>/runs.csv                 one row per document file per run

Documents are keyed on doc_id + file type, so a PDF and its scan (doc_0001.pdf / doc_0001.png)
never share a cache entry. A cached result is reused only if the source file's SHA-256 matches.

Exit codes: 0 ok, 1 some documents failed, 2 stopped (quota / rate limit / repeated errors),
3 configuration error.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
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
    TransientError,
)

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = ROOT / "data" / "samples"
DEFAULT_OUT = ROOT / "data" / "extracted"
GT_DIR = ROOT / "data" / "synthetic" / "ground_truth"
RUNS_COLUMNS = [
    "run_at", "doc_key", "doc_id", "file_type", "input_dir", "template", "model", "prompt_version",
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


def doc_key(doc: Path) -> str:
    """Cache / log key: doc_id + file type, e.g. 'doc_0001.png'."""
    return f"{doc.stem}.{doc.suffix.lower().lstrip('.')}"


def cache_path(out_dir: Path, doc: Path) -> Path:
    return out_dir / f"{doc_key(doc)}.json"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def find_documents(input_dir: Path) -> list[Path]:
    docs = sorted(p for p in input_dir.iterdir() if p.is_file() and p.suffix.lower() in MIME_TYPES)
    keys = [doc_key(p) for p in docs]
    dupes = {k for k in keys if keys.count(k) > 1}  # e.g. doc.jpg + doc.JPG
    if dupes:
        raise ConfigError(f"several files share a doc key in {input_dir}: {sorted(dupes)}")
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


def _write_json_atomic(path: Path, data: dict) -> None:
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def cached_result_is_valid(cache: Path, sha: str) -> bool:
    try:
        return json.loads(cache.read_text(encoding="utf-8")).get("source_sha256") == sha
    except (OSError, ValueError):
        return False


def migrate_legacy_cache(out_dir: Path, doc: Path, sha: str) -> None:
    """Rename a pre-v2 cache file (<doc_id>.json) to <doc_id>.<ext>.json if it came from this file type.

    Legacy results carry no source hash, so the current file's hash is stamped on. (The 7 sample
    results were produced from files byte-identical to those in data/synthetic.)"""
    legacy = out_dir / f"{doc.stem}.json"
    new = cache_path(out_dir, doc)
    if not legacy.is_file() or new.exists():
        return
    try:
        data = json.loads(legacy.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if Path(data.get("source_file", "")).suffix.lower() != doc.suffix.lower():
        return
    data.setdefault("source_sha256", sha)
    _write_json_atomic(new, data)
    legacy.unlink()


def _upgrade_runs_csv(csv_path: Path) -> None:
    """Rewrite runs.csv in place if it was written with an older column set (keeps all rows)."""
    if not csv_path.exists():
        return
    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames == RUNS_COLUMNS:
            return
        rows = list(reader)
    for r in rows:
        if not r.get("doc_key"):
            r["doc_key"] = f"{r.get('doc_id', '')}.{r.get('file_type', '')}"
    tmp = csv_path.with_suffix(".csv.tmp")
    with tmp.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k) or "" for k in RUNS_COLUMNS})
    tmp.replace(csv_path)


def _append_row(csv_path: Path, row: dict) -> None:
    new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RUNS_COLUMNS)
        if new:
            w.writeheader()
        w.writerow({k: row.get(k, "") for k in RUNS_COLUMNS})


def backoff_delay(attempt: int, base: float, cap: float, retry_after: float | None, rng: random.Random) -> float:
    """Exponential backoff with jitter, never shorter than the server's retry hint."""
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
    max_consecutive_errors: int = 5,
    backoff_base_s: float = 10.0,
    backoff_cap_s: float = 120.0,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = print,
) -> RunSummary:
    out_dir = out_root / safe_dirname(extractor.model)
    out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = out_dir / "runs.csv"
    _upgrade_runs_csv(csv_path)
    rng = random.Random()
    summary = RunSummary()
    docs = find_documents(input_dir)
    log(f"{len(docs)} document(s) in {input_dir} -> {out_dir} (model {extractor.model})")

    made_request = False
    consecutive_errors = 0
    for doc in docs:
        key = doc_key(doc)
        sha = sha256_file(doc)
        migrate_legacy_cache(out_dir, doc, sha)
        cache = cache_path(out_dir, doc)
        base_row = {
            "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "doc_key": key,
            "doc_id": doc.stem,
            "file_type": doc.suffix.lower().lstrip("."),
            "input_dir": str(input_dir),
            "template": template_for(doc),
            "model": extractor.model,
        }
        if cache.exists() and not force:
            if cached_result_is_valid(cache, sha):
                summary.cached.append(key)
                _append_row(csv_path, {**base_row, "status": "cached"})
                log(f"  {key}: cached, skipping (use --force to re-run)")
                continue
            log(f"  {key}: cached result is for a different file version, re-extracting")

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
                        f"Still rate limited on {key} after {max_retries} retries. This usually means the "
                        "free-tier daily quota is used up (it resets at midnight Pacific time). "
                        "Re-run later; finished documents are cached."
                    )
                    log(f"  {key}: {msg}")
                    return summary
                wait = backoff_delay(attempt - 1, backoff_base_s, backoff_cap_s, e.retry_after_s, rng)
                log(f"  {key}: rate limited (attempt {attempt}), retrying in {wait:.0f}s")
                sleep(wait)
                continue
            except TransientError as e:
                if attempt <= max_retries:
                    wait = backoff_delay(attempt - 1, backoff_base_s, backoff_cap_s, e.retry_after_s, rng)
                    log(f"  {key}: transient error (attempt {attempt}), retrying in {wait:.0f}s: {e}")
                    sleep(wait)
                    continue
                _fail(summary, csv_path, base_row, attempt, "error", f"gave up after {attempt} attempts: {e}", log)
            except QuotaExhaustedError as e:
                _append_row(csv_path, {**base_row, "attempts": attempt, "status": "quota_exhausted", "error": str(e)})
                summary.stopped_reason = (
                    f"Daily quota exhausted at {key}. It resets at midnight Pacific time. "
                    "Re-run later; finished documents are cached and will be skipped."
                )
                log(f"  {key}: quota exhausted")
                return summary
            except ResponseParseError as e:
                (out_dir / f"{key}.raw.txt").write_text(e.raw_response, encoding="utf-8")
                _fail(summary, csv_path, base_row, attempt, "parse_error", str(e), log)
            except NotImplementedError as e:
                raise ConfigError(str(e)) from None
            except Exception as e:  # extractor errors are already redacted; keep going with the next doc
                _fail(summary, csv_path, base_row, attempt, "error", str(e), log)
            else:
                consecutive_errors = 0
                data = result.to_json_dict()
                data["source_sha256"] = sha
                _write_json_atomic(cache, data)
                _append_row(csv_path, {
                    **base_row,
                    "prompt_version": result.prompt_version,
                    "latency_s": f"{result.latency_s:.3f}",
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "attempts": attempt,
                    "status": "ok",
                })
                summary.ok.append(key)
                log(f"  {key}: ok in {result.latency_s:.1f}s ({result.input_tokens} in / {result.output_tokens} out)")
                break
            # failure path
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                summary.stopped_reason = (
                    f"{consecutive_errors} documents failed in a row (last: {key}); stopping to avoid "
                    "burning quota on a systematic problem. Check runs.csv, fix, and re-run."
                )
                return summary
            break
    return summary


def _fail(summary: RunSummary, csv_path: Path, base_row: dict, attempt: int, status: str, msg: str, log) -> None:
    _append_row(csv_path, {**base_row, "attempts": attempt, "status": status, "error": msg})
    summary.failed.append(base_row["doc_key"])
    log(f"  {base_row['doc_key']}: {status}: {msg}")


def main(argv: list[str] | None = None) -> int:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--input", type=Path, default=DEFAULT_INPUT, help="folder with PDFs / PNGs (default data/samples)")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT, help="output root (default data/extracted)")
    p.add_argument("--force", action="store_true", help="re-run documents that already have a cached result")
    p.add_argument("--delay", type=float, default=float(os.environ.get("LLM_REQUEST_DELAY_S", "10")),
                   help="seconds between API requests (default LLM_REQUEST_DELAY_S or 10)")
    p.add_argument("--max-retries", type=int, default=5, help="retries per document on rate-limit / 5xx errors")
    p.add_argument("--max-consecutive-errors", type=int, default=5,
                   help="stop the run after this many documents fail in a row")
    args = p.parse_args(argv)

    if not args.input.is_dir():
        print(f"Input folder not found: {args.input}", file=sys.stderr)
        return 3
    try:
        extractor = get_extractor()
        summary = run(extractor, args.input, args.out, force=args.force, delay_s=args.delay,
                      max_retries=args.max_retries, max_consecutive_errors=args.max_consecutive_errors)
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
