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

Only the first building block exists so far: a **reproducible synthetic dataset with ground truth**. See [Status](#status).

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

The tests check that:
- normal invoices add up
- every anomaly really breaks its rule
- every PDF has a ground-truth file (and vice versa)
- the ratios and coverage are as expected
- the samples are complete
- the generator is byte-for-byte reproducible

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
docs/                design notes (upcoming)
src/                 code
tests/               pytest suite
```

## Status

- [x] Project scaffold (venv, pinned deps, docker-compose for Postgres, `.env.example`)
- [x] Synthetic invoice generator: 5 templates incl. bilingual AR/EN, scans, anomalies, ground truth
- [x] Dataset tests
- [ ] LLM extraction to structured JSON
- [ ] Validation rules and human review queue
- [ ] Load to PostgreSQL
- [ ] dbt models
- [ ] Metabase spend dashboard
- [ ] Accuracy report against ground truth
- [ ] n8n workflow wrapping the pipeline

## Licences

- Fonts: Amiri © The Amiri Project Authors, SIL Open Font License 1.1 (`assets/fonts/OFL.txt`).
