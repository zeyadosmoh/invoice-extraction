#!/usr/bin/env python3
"""Build (or update) the "Invoice Spend" Metabase dashboard via the API.

Idempotent: the database connection, collection, cards and dashboard are looked up by name and
updated in place, so running it again never creates duplicates.

Reuses the Metabase from the olist-analytics project (http://localhost:3000). Metabase runs in Docker
and reaches invoice_pg through host.docker.internal:5434, connecting as the read-only role
metabase_ro (scripts/create_metabase_ro.sh), which can only read the `marts` schema.

Reads from .env (never committed, values never printed):
    MB_API_KEY            Metabase admin API key
    MB_URL                optional, default http://localhost:3000
    METABASE_RO_PASSWORD  password of the metabase_ro Postgres role
    POSTGRES_DB, POSTGRES_PORT
    MB_PG_HOST            optional, host as seen from the Metabase container (default host.docker.internal)

Usage:
    .venv/bin/python scripts/metabase/build_spend_dashboard.py
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

REPO_ROOT = Path(__file__).resolve().parents[2]
COLLECTION_NAME = "Invoice Extraction"
DASHBOARD_NAME = "Invoice Spend"
MB_DB_NAME = "Invoice Extraction (invoices)"  # display name of the connection in Metabase
FX_NOTE = ("USD uses FIXED ILLUSTRATIVE rates as of 2025-12-31 (synthetic project; "
           "dbt/seeds/fx_rates_usd.csv), not market rates.")


# --------------------------------------------------------------------------
# Config + HTTP helpers
# --------------------------------------------------------------------------
def load_env() -> None:
    """Load KEY=VALUE pairs from .env into os.environ (env vars win)."""
    env_file = REPO_ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


class Metabase:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"x-api-key": api_key})

    def call(self, method: str, path: str, **kwargs):
        resp = self.session.request(method, f"{self.url}/api{path}", timeout=120, **kwargs)
        if resp.status_code >= 400:
            # Response bodies never echo the API key; truncate anyway.
            sys.exit(f"ERROR {method} {path} -> {resp.status_code}: {resp.text[:300]}")
        return resp.json() if resp.content else None

    def get(self, path, **kw):
        return self.call("GET", path, **kw)

    def post(self, path, **kw):
        return self.call("POST", path, **kw)

    def put(self, path, **kw):
        return self.call("PUT", path, **kw)


# --------------------------------------------------------------------------
# Database connection, collection
# --------------------------------------------------------------------------
def db_details() -> dict:
    password = os.environ.get("METABASE_RO_PASSWORD")
    if not password:
        sys.exit("ERROR: METABASE_RO_PASSWORD is not set (add it to .env).")
    return {
        "host": os.environ.get("MB_PG_HOST", "host.docker.internal"),
        "port": int(os.environ.get("POSTGRES_PORT", "5434")),
        "dbname": os.environ.get("POSTGRES_DB", "invoices"),
        "user": "metabase_ro",
        "password": password,
        "ssl": False,
        "schema-filters-type": "inclusion",
        "schema-filters-patterns": "marts",
    }


def upsert_database(mb: Metabase) -> int:
    dbs = mb.get("/database")
    dbs = dbs.get("data", dbs)
    body = {"name": MB_DB_NAME, "engine": "postgres", "details": db_details(),
            "is_full_sync": True, "auto_run_queries": True}
    for db in dbs:
        if db["name"] == MB_DB_NAME:
            mb.put(f"/database/{db['id']}", json=body)
            print(f"  updated database connection {db['id']}  {MB_DB_NAME}")
            return db["id"]
    db_id = mb.post("/database", json=body)["id"]
    print(f"  created database connection {db_id}  {MB_DB_NAME}")
    return db_id


def sync_and_wait(mb: Metabase, db_id: int, tables: set[str], timeout_s: int = 120) -> None:
    """Ask Metabase to rescan the schema, then wait until every mart table is visible."""
    mb.post(f"/database/{db_id}/sync_schema")
    deadline = time.time() + timeout_s
    while True:
        meta = mb.get(f"/database/{db_id}/metadata")
        seen = {t["name"] for t in meta["tables"] if t["schema"] == "marts"}
        if tables <= seen:
            print(f"  synced: {len(seen)} marts tables visible")
            return
        if time.time() > deadline:
            sys.exit(f"ERROR: Metabase has not synced these marts tables yet: {sorted(tables - seen)}")
        time.sleep(3)


def get_or_create_collection(mb: Metabase) -> int:
    for c in mb.get("/collection"):
        if c.get("name") == COLLECTION_NAME and not c.get("archived") and not c.get("personal_owner_id"):
            return c["id"]
    created = mb.post("/collection", json={
        "name": COLLECTION_NAME,
        "description": "Invoice extraction project (synthetic data): questions built on the dbt marts schema.",
    })
    print(f"  created collection '{COLLECTION_NAME}' (id {created['id']})")
    return created["id"]


def collection_items(mb: Metabase, coll_id: int, model: str) -> dict[str, int]:
    items = mb.get(f"/collection/{coll_id}/items", params={"models": model})
    items = items.get("data", items)
    return {i["name"]: i["id"] for i in items}


# --------------------------------------------------------------------------
# Card definitions (native SQL on the marts schema)
# --------------------------------------------------------------------------
def col(name: str) -> str:
    return f'["name","{name}"]'


USD = {"number_style": "currency", "currency": "USD", "currency_style": "symbol"}
MONEY = {"number_style": "decimal", "decimals": 2}

CARDS = [
    dict(
        key="spend_month", name="Approved spend by month (USD)", display="bar",
        description=f"Sum of invoice totals (incl. tax) of approved invoices, by invoice month. {FX_NOTE}",
        sql="""select invoice_month as month, sum(spend_usd) as spend_usd, sum(n_invoices) as invoices
from marts.mart_spend_monthly
group by invoice_month
order by invoice_month""",
        viz={
            "graph.dimensions": ["month"],
            "graph.metrics": ["spend_usd"],
            "graph.tooltip_columns": [col("invoices")],
            "graph.x_axis.title_text": "Invoice month",
            "graph.y_axis.title_text": "Approved spend (USD)",
            "column_settings": {col("spend_usd"): {**USD, "decimals": 0, "column_title": "Spend (USD)"},
                                col("month"): {"date_style": "MMMM, YYYY", "date_abbreviate": True}},
        },
    ),
    dict(
        key="top_vendors", name="Top 10 vendors by approved spend (USD)", display="row",
        description="Approved invoices only; vendors under their canonical name "
                    f"(dbt/seeds/vendor_aliases.csv). {FX_NOTE}",
        sql="""select vendor_name as vendor, sum(spend_usd) as spend_usd
from marts.mart_spend_monthly
group by vendor_name
order by spend_usd desc
limit 10""",
        viz={
            "graph.dimensions": ["vendor"],
            "graph.metrics": ["spend_usd"],
            "graph.show_values": True,
            "column_settings": {col("spend_usd"): {**USD, "decimals": 0, "column_title": "Spend (USD)"}},
        },
    ),
    dict(
        key="spend_currency", name="Spend by currency (original currency)", display="table",
        description="Approved invoices only, in the currency printed on the invoice. Amounts in "
                    f"different currencies are not summed together; the USD column is for comparison. {FX_NOTE}",
        sql="""select
    currency,
    sum(n_invoices)     as invoices,
    sum(spend_original) as spend_original,
    sum(spend_usd)      as spend_usd
from marts.mart_spend_monthly
group by currency
order by spend_usd desc""",
        viz={
            "column_settings": {
                col("currency"): {"column_title": "Currency"},
                col("invoices"): {"column_title": "Invoices"},
                col("spend_original"): {**MONEY, "column_title": "Spend (original currency)"},
                col("spend_usd"): {**USD, "decimals": 2, "column_title": "Spend (USD, illustrative)"},
            },
        },
    ),
    dict(
        key="review_count", name="Open review items", display="scalar",
        description="Invoices that failed at least one validation rule and wait for a human reviewer.",
        sql="select count(*) as open_items from marts.mart_review_queue",
        viz={},
    ),
    dict(
        key="review_amount", name="Amount at stake in review (USD)", display="scalar",
        description=f"Sum of the totals of the invoices waiting for review. {FX_NOTE}",
        sql="select coalesce(sum(amount_at_stake_usd), 0) as amount_at_stake_usd from marts.mart_review_queue",
        viz={"scalar.compact_primary_number": False,
             "column_settings": {col("amount_at_stake_usd"): {**USD, "decimals": 0}}},
    ),
    dict(
        key="review_table", name="Review queue", display="table",
        description="One row per invoice waiting for review, with every failed rule and its reason.",
        sql="""select
    doc_id,
    invoice_number,
    vendor_name,
    invoice_date,
    currency,
    amount_at_stake,
    amount_at_stake_usd,
    reasons,
    source_file
from marts.mart_review_queue
order by amount_at_stake_usd desc""",
        viz={
            "table.cell_column": "amount_at_stake_usd",
            "column_settings": {
                col("doc_id"): {"column_title": "Doc"},
                col("invoice_number"): {"column_title": "Invoice"},
                col("vendor_name"): {"column_title": "Vendor"},
                col("invoice_date"): {"column_title": "Invoice date"},
                col("currency"): {"column_title": "Currency"},
                col("amount_at_stake"): {**MONEY, "column_title": "Amount"},
                col("amount_at_stake_usd"): {**USD, "decimals": 2, "column_title": "Amount (USD)"},
                col("reasons"): {"column_title": "Reasons"},
                col("source_file"): {"column_title": "File"},
            },
        },
    ),
    dict(
        key="tax_currency", name="Tax collected by currency", display="table",
        description="Tax (VAT) charged on approved invoices, in the invoice currency. "
                    f"{FX_NOTE}",
        sql="""select
    currency,
    sum(n_invoices)   as invoices,
    sum(tax_original) as tax_original,
    sum(tax_usd)      as tax_usd
from marts.mart_spend_monthly
group by currency
order by tax_usd desc""",
        viz={
            "column_settings": {
                col("currency"): {"column_title": "Currency"},
                col("invoices"): {"column_title": "Invoices"},
                col("tax_original"): {**MONEY, "column_title": "Tax (original currency)"},
                col("tax_usd"): {**USD, "decimals": 2, "column_title": "Tax (USD, illustrative)"},
            },
        },
    ),
]

INTRO_TEXT = f"""**About this dashboard** (synthetic data: every vendor, buyer and amount is fictional)
- Invoices extracted by an LLM from PDFs and scans, validated by rules, loaded to Postgres and modeled with dbt (`marts` schema).
- **Spend** = invoice total incl. tax, **approved invoices only** (no failed validation rule). Invoices that failed a rule are in the review queue.
- {FX_NOTE}"""
TEXT_KEY = "intro_text"

# Grid is 24 columns wide. (card key, row, col, width, height)
LAYOUT = [
    (TEXT_KEY, 0, 0, 24, 3),
    ("review_count", 3, 0, 6, 3),
    ("review_amount", 3, 6, 6, 3),
    ("spend_currency", 3, 12, 12, 5),
    ("spend_month", 8, 0, 24, 7),
    ("top_vendors", 15, 0, 12, 8),
    ("tax_currency", 15, 12, 12, 5),
    ("review_table", 23, 0, 24, 8),
]


# --------------------------------------------------------------------------
# Build
# --------------------------------------------------------------------------
def upsert_cards(mb: Metabase, db_id: int, coll_id: int) -> dict[str, int]:
    existing = collection_items(mb, coll_id, "card")
    ids = {}
    for card in CARDS:
        body = {
            "name": card["name"],
            "description": card["description"],
            "display": card["display"],
            "collection_id": coll_id,
            "visualization_settings": card["viz"],
            "dataset_query": {
                "type": "native",
                "database": db_id,
                "native": {"query": card["sql"].strip(), "template-tags": {}},
            },
        }
        if card["name"] in existing:
            cid = existing[card["name"]]
            mb.put(f"/card/{cid}", json=body)
            print(f"  updated card {cid:>4}  {card['name']}")
        else:
            cid = mb.post("/card", json=body)["id"]
            print(f"  created card {cid:>4}  {card['name']}")
        ids[card["key"]] = cid
    return ids


def upsert_dashboard(mb: Metabase, coll_id: int, card_ids: dict[str, int]) -> int:
    existing = collection_items(mb, coll_id, "dashboard")
    desc = ("Approved spend, tax and the human review queue for the synthetic invoice dataset. "
            "Built by scripts/metabase/build_spend_dashboard.py from the dbt marts schema - "
            "edit the script, not the UI.")
    if DASHBOARD_NAME in existing:
        dash_id = existing[DASHBOARD_NAME]
        print(f"  found dashboard {dash_id}  {DASHBOARD_NAME}")
    else:
        dash_id = mb.post("/dashboard", json={
            "name": DASHBOARD_NAME, "collection_id": coll_id, "description": desc,
        })["id"]
        print(f"  created dashboard {dash_id}  {DASHBOARD_NAME}")

    # Reuse existing dashcard ids so the update is in place.
    current = mb.get(f"/dashboard/{dash_id}").get("dashcards", [])
    dashcard_id_by_card = {dc["card_id"]: dc["id"] for dc in current if dc.get("card_id")}
    text_ids = [dc["id"] for dc in current if not dc.get("card_id")]

    dashcards = []
    for i, (key, row, column, width, height) in enumerate(LAYOUT):
        if key == TEXT_KEY:
            dashcards.append({
                "id": text_ids[0] if text_ids else -(i + 1),
                "card_id": None,
                "row": row, "col": column, "size_x": width, "size_y": height,
                "parameter_mappings": [],
                "visualization_settings": {
                    "virtual_card": {"name": None, "display": "text", "archived": False,
                                     "dataset_query": {}, "visualization_settings": {}},
                    "text": INTRO_TEXT,
                },
            })
            continue
        cid = card_ids[key]
        dashcards.append({
            "id": dashcard_id_by_card.get(cid, -(i + 1)),  # negative id = new
            "card_id": cid,
            "row": row, "col": column, "size_x": width, "size_y": height,
            "parameter_mappings": [],
            "visualization_settings": {},
        })

    mb.put(f"/dashboard/{dash_id}", json={
        "name": DASHBOARD_NAME, "description": desc, "collection_id": coll_id,
        "parameters": [], "dashcards": dashcards,
    })
    print(f"  saved dashboard layout: {len(dashcards)} cards")
    return dash_id


def check_dashboard(mb: Metabase, dash_id: int) -> bool:
    """Run every card on the dashboard; print row count and first row."""
    dash = mb.get(f"/dashboard/{dash_id}")
    ok = True
    for dc in sorted(dash["dashcards"], key=lambda d: (d["row"], d["col"])):
        if not dc.get("card_id"):
            continue
        cid, name = dc["card_id"], dc["card"]["name"]
        res = mb.post(f"/dashboard/{dash_id}/dashcard/{dc['id']}/card/{cid}/query", json={"parameters": []})
        if res.get("status") != "completed" or res.get("error"):
            ok = False
            print(f"  ! {name}: {res.get('error')}")
            continue
        rows = res["data"]["rows"]
        print(f"  {name:<42} {res['row_count']:>4} rows   first: {rows[0] if rows else None}")
    return ok


def main() -> None:
    load_env()
    api_key = os.environ.get("MB_API_KEY")
    if not api_key:
        sys.exit("ERROR: MB_API_KEY is not set (add it to .env).")
    mb_url = os.environ.get("MB_URL", "http://localhost:3000").rstrip("/")
    mb = Metabase(mb_url, api_key)

    print("1) Database connection")
    db_id = upsert_database(mb)
    sync_and_wait(mb, db_id, {"mart_spend_monthly", "mart_review_queue", "fct_invoices"})

    print("2) Collection")
    coll_id = get_or_create_collection(mb)

    print("3) Cards")
    card_ids = upsert_cards(mb, db_id, coll_id)

    print("4) Dashboard")
    dash_id = upsert_dashboard(mb, coll_id, card_ids)

    print("5) Check")
    ok = check_dashboard(mb, dash_id)
    print(f"\nDashboard: {mb_url}/dashboard/{dash_id}")
    if not ok:
        sys.exit("ERROR: at least one card failed.")


if __name__ == "__main__":
    main()
