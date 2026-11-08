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

So far: a **reproducible synthetic dataset with ground truth** and a provider-agnostic **LLM extraction module** (Gemini), tested on the samples. See [Status](#status).

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
- digit and number normalisation works, and a sample response parses into the schema
- normal invoices add up
- every anomaly really breaks its rule
- every PDF has a ground-truth file (and vice versa)
- the ratios and coverage are as expected
- the samples are complete
- the generator is byte-for-byte reproducible

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
- Results are cached in `data/extracted/<model>/<doc_id>.json`; documents with a result are skipped unless `--force`. Each run appends one row per document to `data/extracted/<model>/runs.csv` (file type, template, latency, tokens, status, error). `data/extracted/` is gitignored.
- Free tier: requests are spaced by `LLM_REQUEST_DELAY_S` (default 10 s). Rate-limit errors are retried with jittered exponential backoff that honours the server's retry hint. On a daily-quota error (or a rate limit that persists through all retries) the run stops with a message and exit code 2; finished documents stay cached.
- Names are returned **as printed**. The T3 receipt prints the vendor name in upper case, so extraction (and the ground truth) gives `ATELIER SOLENNE SARL` where other templates give `Atelier Solenne SARL`. Mapping printed vendor names to one canonical vendor happens later, in dbt, not in extraction.
- The API key is read from `.env` by python-dotenv and is never printed or logged. Error messages are scrubbed before they reach the console or `runs.csv`.

### Database (for later phases)

`docker-compose.yml` defines PostgreSQL 16 as the container `invoice_pg`, bound to `127.0.0.1:5434`, with credentials from `.env`:

```bash
docker compose up -d
```

## Repository layout

```
assets/fonts/        Amiri (SIL Open Font License, see OFL.txt)
data/samples/        curated, committed examples
data/synthetic/      generated dataset (pdf/ and scans/ are gitignored)
data/extracted/      LLM extraction results + runs.csv per model (gitignored)
docs/                design notes (upcoming)
src/                 code: generate_invoices.py, run_extraction.py, compare_samples.py
src/extract/         extractor interface, Gemini implementation, schema, normalisation
src/prompts/         versioned prompts
tests/               pytest suite
```

## Status

- [x] Project scaffold (venv, pinned deps, docker-compose for Postgres, `.env.example`)
- [x] Synthetic invoice generator: 5 templates incl. bilingual AR/EN, scans, anomalies, ground truth
- [x] Dataset tests
- [x] LLM extraction to structured JSON (Gemini, provider-agnostic interface; tested on the 7 samples, full 120-doc run pending)
- [ ] Validation rules and human review queue
- [ ] Load to PostgreSQL
- [ ] dbt models
- [ ] Metabase spend dashboard
- [ ] Accuracy report against ground truth
- [ ] n8n workflow wrapping the pipeline

## Licences

- Fonts: Amiri © The Amiri Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
