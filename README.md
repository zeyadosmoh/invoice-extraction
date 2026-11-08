# AI Invoice & Document Extraction

Portfolio project: turn invoices (PDFs and scanned images) into validated, structured data and a spend dashboard, with a human review queue for anything the pipeline is unsure about and an accuracy report measured against ground truth.

> All data in this repository is **synthetic**. Every vendor, buyer, address, tax ID and invoice is made up by `src/generate_invoices.py`. Any resemblance to real organisations is coincidental.

## Target architecture

```
PDF / scanned image
      │
      ▼
 LLM extraction ──► structured JSON ──► validation rules ──┬──► PostgreSQL ──► dbt ──► Metabase spend dashboard
                                                           │
                                                           └──► human review queue (uncertain / anomalous)

 accuracy report: extracted JSON vs. ground truth          n8n workflow wraps the whole flow
```

Done so far: a **reproducible synthetic dataset with ground truth**, a provider-agnostic **LLM extraction module** (Gemini), **validation rules with a review queue** and an **accuracy report**. Results are **loaded to PostgreSQL**, modeled with **dbt** and shown in a **Metabase spend dashboard**. See [Status](#status).

## Sample invoices

You can browse examples without running anything. See **[data/samples/](data/samples/)** for one PDF per layout template plus two scanned images, each with its ground-truth JSON:

| Template | Example | Ground truth |
|---|---|---|
| T1 classic | [doc_0002.pdf](data/samples/doc_0002.pdf) | [json](data/samples/doc_0002.json) |
| T2 modern | [doc_0003.pdf](data/samples/doc_0003.pdf) | [json](data/samples/doc_0003.json) |
| T3 receipt (monospace) | [doc_0022.pdf](data/samples/doc_0022.pdf) | [json](data/samples/doc_0022.json) |
| T4 two-column | [doc_0032.pdf](data/samples/doc_0032.pdf) | [json](data/samples/doc_0032.json) |
| T5 bilingual Arabic/English (Arabic-Indic digits) | [doc_0045.pdf](data/samples/doc_0045.pdf) | [json](data/samples/doc_0045.json) |
| Scan (bilingual) | [doc_0001.png](data/samples/doc_0001.png) | [json](data/samples/doc_0001.json) |
| Scan (anomaly: missing invoice date) | [doc_0047.png](data/samples/doc_0047.png) | [json](data/samples/doc_0047.json) |

## The synthetic dataset

`src/generate_invoices.py` produces (seed `42`, fully reproducible, byte-identical PDFs):

| | |
|---|---|
| Invoices | 120 PDFs from 15 fictional vendors |
| Layouts | 5 templates (24 each). The labels, column order, date formats and number formats all differ (`1,234.56` vs `1.234,56`, `2025-03-14` vs `March 14, 2025` vs `14.03.2025`) |
| Bilingual | T5 is Arabic/English (Amiri font). About half of the T5 invoices print amounts, quantities and dates in Arabic-Indic digits (`٠١٢٣٤٥٦٧٨٩`) |
| Currencies | USD, EUR, SAR, AED, EGP, each with a realistic tax rate (sales tax / VAT) |
| Line items | 1 to 12 per invoice; about 25% of invoices have a discount |
| Scans | 36 (30%) are also rendered as scanned-looking PNGs at 100–150 DPI, with ±2.5° rotation, blur, noise, speckles and JPEG artefacts |
| Anomalies | 12 (10%), 3 of each type: `total_mismatch`, `missing_invoice_date`, `duplicate_invoice_number`, `tax_amount_mismatch` |

### Ground-truth format

One JSON per invoice in `data/synthetic/ground_truth/doc_NNNN.json`. The ground truth stores the values **as printed** on the document. For anomalous documents this means it is faithfully wrong, and `anomaly.reason` explains how.

```jsonc
{
  "doc_id": "doc_0002",
  "invoice": {                       // the extraction target
    "vendor_name": "...", "vendor_name_ar": null, "vendor_tax_id": "...", "buyer_name": "...",
    "invoice_number": "...", "invoice_date": "2025-01-03",   // null if not printed
    "due_date": "2025-02-02", "currency": "USD",
    "line_items": [{"description": "...", "description_ar": null,
                    "quantity": "3", "unit_price": "18.90", "line_total": "56.70"}],
    "subtotal": "...", "discount": null, "tax_rate": "8.25", "tax_amount": "...", "total": "..."
  },
  "is_anomaly": false,
  "anomaly": null,                   // or {"type": "...", "reason": "...", ...}
  "meta": {"vendor_id": "V01", "template": "T1_classic", "language": "en", "digits": "western",
           "files": {"pdf": "pdf/doc_0002.pdf", "png": null}, "scan": null, "generator_seed": 42}
}
```

Conventions:
- Money, quantities and rates are decimal **strings** (exact, no float error) and always use Western digits, whatever the document prints.
- `tax_rate` is a percentage.
- `discount` is an amount, or `null` when no discount is printed.
- Arithmetic: `line_total = round(qty × unit_price, 2)`, `subtotal = Σ line_total`, `tax = round((subtotal − discount) × rate / 100, 2)`, and `total = subtotal − discount + tax`, all rounded half-up.

`data/synthetic/manifest.csv` has one summary row per invoice: template, digits, currency, total, anomaly type and file paths.

## Setup

Requires Python 3.14 (dependencies are pinned in `requirements.txt`; no system packages needed).

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then edit the values locally; .env is gitignored
```

### Generate the dataset

```bash
python src/generate_invoices.py              # defaults: --seed 42 --n 120
python src/generate_invoices.py --help       # options: --seed, --n, --out, --scan-ratio, --anomaly-ratio, --no-samples
```

This takes about 15 s. It writes `data/synthetic/{pdf,scans,ground_truth}/` and `manifest.csv`, and refreshes `data/samples/`. The generated PDFs and PNGs (~35 MB) are gitignored because one command reproduces them. Ground truth, the manifest and the curated samples are committed.

### Run the tests

```bash
pytest -q
```

The tests run offline (no API calls). They check that:
- the extraction runner caches, throttles, retries, and stops on quota (using a fake extractor)
- digit and number normalisation works (discounts always stored positive), and a sample response parses into the schema
- every validation rule passes clean invoices and fails broken ones with a reason; skipped rules don't block approval
- normal invoices add up
- every anomaly really breaks its rule
- every PDF has a ground-truth file (and vice versa)
- the ratios and coverage are as expected
- the samples are complete
- the generator is byte-for-byte reproducible
- the dbt vendor seed maps every printed vendor name to its ground-truth vendor_id, one-to-one
- loading twice into Postgres gives the same row counts (skipped when `invoice_pg` is not running)

### LLM extraction

Set `GEMINI_API_KEY` in `.env` (a free key from Google AI Studio). `LLM_PROVIDER` and `LLM_MODEL` pick the provider and model; the default is `gemini` / `gemini-3.5-flash-lite`, which is on the free tier and accepts PDF and image input with structured JSON output. Claude and OpenAI extractors exist only as stubs.

```bash
python src/run_extraction.py                 # extracts data/samples/ (7 docs)
python src/run_extraction.py --force         # ignore the cache and re-run
python src/run_extraction.py --input data/synthetic/pdf --delay 15
python src/compare_samples.py                # field-by-field diff vs ground truth (console only)
```

How it works:
- PDFs are sent to the model as PDFs and PNGs as images, with the versioned prompt in [`src/prompts/extract_invoice.md`](src/prompts/extract_invoice.md) and a JSON schema. Amounts and dates come back as strings, so nothing is rounded through floats.
- The response is passed through a normalisation safety net (`src/extract/normalize.py`: Arabic-Indic digits, Arabic separators, `1.234,56`-style numbers, printed dates to ISO) and validated into the Pydantic model in `src/extract/schema.py`. Money is `Decimal`, dates are `date`, a missing field is `null`, and `uncertain_fields` lists what the model could not read clearly.
- Results are cached in `data/extracted/<model>/<doc_id>.<ext>.json`, keyed on doc_id + file type, so a PDF and its scan (`doc_0001.pdf.json`, `doc_0001.png.json`) never overwrite or skip each other. A cached result is reused only if the source file's SHA-256 still matches; `--force` re-runs everything. Each run appends one row per document to `data/extracted/<model>/runs.csv` (doc key, file type, input folder, template, latency, tokens, status, error). `data/extracted/` is gitignored.
- Free tier: requests are spaced by `LLM_REQUEST_DELAY_S` (default 10 s). Rate-limit errors are retried with jittered exponential backoff that honours the server's retry hint. Server errors (5xx) get the same bounded retry, then the document is marked failed and the run moves on. On a daily-quota error, a rate limit that persists through all retries, or 5 failed documents in a row, the run stops with a message and exit code 2; finished documents stay cached.
- Names are returned **as printed**. The T3 receipt prints the vendor name in upper case, so extraction (and the ground truth) gives `ATELIER SOLENNE SARL` where other templates give `Atelier Solenne SARL`. Mapping printed vendor names to one canonical vendor happens later, in dbt, not in extraction.
- The API key is read from `.env` by python-dotenv and is never printed or logged. Error messages are scrubbed before they reach the console or `runs.csv`.

### Validation, review queue and accuracy report

Both scripts work only from cached extraction results: no API calls.

```bash
python src/validate.py      # rules on every cached result -> data/review/queue.csv (gitignored)
python src/report.py        # docs/accuracy_report.md (committed)
```

- Each cached result is re-parsed from the model's `raw_response` with the current normalisation, so a new normalisation rule applies without re-running the model.
- Rules (`src/validate.py`), tolerance 0.01: line total = qty x unit price; subtotal = sum of lines; tax = (subtotal - discount) x rate; total = subtotal - discount + tax; required fields (invoice number, vendor, invoice date, total, currency); due date >= invoice date; invoice date not in the future; duplicate (same vendor + invoice number already received); date sequence (per vendor, a higher invoice number must not have an earlier date; catches day/month swaps that move a date past its neighbours).
- Each rule returns pass, fail or skip (couldn't run, e.g. no tax printed). Any fail -> `needs_review` with all reasons; skips count as pass but are listed in the `skipped_rules` column.
- Duplicate and sequence rules compare a document with the rest of its batch in doc_id order. Scans are checked in a batch where those invoices arrive as scans, so a scan is never compared with its own PDF. Invoices that fail the duplicate rule are left out of the sequence comparison.
- [`docs/accuracy_report.md`](docs/accuracy_report.md): field accuracy (before/after normalisation fixes, per field, template, PDF vs scan, digits), review-queue recall and false alarms, cost per 1,000 invoices from the official price page, latency, limitations.

### Database: load to PostgreSQL

`docker-compose.yml` defines PostgreSQL 16 as the container `invoice_pg`, bound to `127.0.0.1:5434`, with credentials from `.env`.

```bash
docker compose up -d
python src/load.py                       # cached results only, no API calls
bash scripts/create_metabase_ro.sh       # read-only role for Metabase (once; idempotent)
```

`src/load.py` uses the same code path as `src/validate.py` and loads the validator's "inbox" view: one row per invoice, the scan where one exists, otherwise the PDF (120 invoices: 36 scans, 84 PDFs). Values are the normalised ones. Every re-run truncates and refills the tables in one transaction, so data is replaced, never duplicated, and dbt's staging views on top keep working. After changing a table definition, run it once with `--recreate`: that drops the raw tables CASCADE, and the next `dbt build` recreates the views.

| table | grain | notes |
|---|---|---|
| `raw.invoices` | one per invoice | header fields, `status` (`auto_approved` / `needs_review`), `failed_reasons`, `skipped_rules`, `source_file`, `model`, `prompt_version`, `validated_as_of`, `loaded_at` |
| `raw.invoice_lines` | one per line item | `doc_id`, `line_no`, description, quantity, unit price, line total |
| `raw.review_queue` | one per failed rule | `doc_id`, `rule`, `reason` (only invoices that need review) |

`scripts/create_metabase_ro.sh` creates the role `metabase_ro` (password `METABASE_RO_PASSWORD` in `.env`). It can only read the `marts` schema.

### dbt models

dbt runs in its own venv so its pinned dependencies can't clash with the main one. dbt-core 1.12 and dbt-postgres 1.11 support Python 3.14, so it uses the same Python:

```bash
python3.14 -m venv .venv-dbt
.venv-dbt/bin/pip install -r requirements-dbt.txt
set -a; source .env; set +a              # dbt/profiles.yml reads connection settings from env vars
cd dbt && ../.venv-dbt/bin/dbt build --profiles-dir .
```

| layer | schema | models |
|---|---|---|
| seeds | `seeds` | `vendor_aliases` (20 printed names -> 15 vendors), `fx_rates_usd` (5 currencies) |
| staging (views) | `staging` | `stg_invoices`, `stg_invoice_lines`, `stg_review_queue` |
| marts (tables) | `marts` | `dim_vendors`, `fct_invoices`, `fct_invoice_lines`, `mart_spend_monthly` (approved invoices only; month x vendor x currency, original and USD), `mart_review_queue` (open items: all reasons and amount at stake) |

- **Vendors.** Names are extracted as printed, so `ATELIER SOLENNE SARL` (receipt template) and `Atelier Solenne SARL` are the same vendor. `seeds/vendor_aliases.csv` maps each printed name to the ground-truth `vendor_id` and one canonical name. All 15 vendors in the generator appear in the 120 invoices; 5 of them have an upper-case variant. Tests fail if any printed name has no mapping, or if a canonical name covers more than one `vendor_id` (or the reverse). `tests/test_dbt_seeds.py` checks the seed against the ground truth.
- **Currency.** `seeds/fx_rates_usd.csv` holds **fixed illustrative rates as of 2025-12-31**, used only so all spend can be shown in USD. This is a synthetic project, so these are not market rates. AED and SAR use their USD pegs; EUR (1.08) and EGP (0.0205) are round numbers. Original-currency amounts are always kept alongside USD.
- **Tests** (66 in total, including seeds): unique / not_null / relationships / accepted_values on keys and statuses, plus reconciliation tests:
  - per currency, invoice count and sum of totals in `fct_invoices` = `raw.invoices`
  - every raw invoice is exactly one of: approved, or in `mart_review_queue`

### Metabase spend dashboard

The dashboard reuses the Metabase from the sibling olist-analytics project (`olist_metabase`, `http://localhost:3000`) without changing that project's files. Metabase runs in Docker Desktop and reaches this database at `host.docker.internal:5434` as `metabase_ro`.

```bash
docker start olist_metabase
python scripts/metabase/build_spend_dashboard.py
```

The script is idempotent: it creates or updates the database connection "Invoice Extraction (invoices)", the collection "Invoice Extraction", 7 native-SQL cards and the dashboard **Invoice Spend**. It then runs every card as a check. It needs `MB_API_KEY` (Metabase admin API key) and `METABASE_RO_PASSWORD` in `.env`; neither is ever printed. Edit the script, not the UI.

Cards:
- intro text
- open review items
- amount at stake in review (USD)
- spend by currency (original currency)
- approved spend by month (USD)
- top 10 vendors by approved spend (USD)
- tax collected by currency
- review queue table (invoice, vendor, amount, reasons)

"Spend" = invoice total incl. tax, approved invoices only.

## Repository layout

```
assets/fonts/        Amiri (SIL Open Font License, see OFL.txt)
data/samples/        curated, committed examples
data/synthetic/      generated dataset (pdf/ and scans/ are gitignored)
data/extracted/      LLM extraction results + runs.csv per model (gitignored)
data/review/         review queue, queue.csv (gitignored, rebuilt by src/validate.py)
dbt/                 dbt project: seeds/, models/staging, models/marts, tests/ (singular + reconciliation)
docs/                accuracy_report.md (generated), run_notes.md
scripts/             create_metabase_ro.sh; metabase/build_spend_dashboard.py
src/                 code: generate_invoices.py, run_extraction.py, compare_samples.py, validate.py, report.py, load.py
src/extract/         extractor interface, Gemini implementation, schema, normalisation
src/prompts/         versioned prompts
tests/               pytest suite
```

## Status

- [x] Project scaffold (venv, pinned deps, docker-compose for Postgres, `.env.example`)
- [x] Synthetic invoice generator: 5 templates incl. bilingual AR/EN, scans, anomalies, ground truth
- [x] Dataset tests
- [x] LLM extraction to structured JSON (Gemini, provider-agnostic interface; full run done: 120 PDFs + 36 scans)
- [x] Validation rules and human review queue (`data/review/queue.csv`)
- [x] Accuracy report against ground truth ([`docs/accuracy_report.md`](docs/accuracy_report.md))
- [x] Load to PostgreSQL (`src/load.py`: `raw.invoices`, `raw.invoice_lines`, `raw.review_queue`; idempotent)
- [x] dbt models: staging, vendor + FX seeds, marts, 66 tests incl. reconciliation (all passing)
- [x] Metabase spend dashboard (`scripts/metabase/build_spend_dashboard.py`, reuses the olist-analytics Metabase)
- [ ] n8n workflow wrapping the pipeline

## Licences

- Fonts: Amiri © The Amiri Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
