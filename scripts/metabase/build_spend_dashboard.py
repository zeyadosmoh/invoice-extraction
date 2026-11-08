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


# One color for every bar chart.
BAR_COLOR = "#2F6DB5"
USD = {"number_style": "currency", "currency": "USD", "currency_style": "symbol",
       "currency_in_header": False}
USD_CODE = {"number_style": "currency", "currency": "USD", "currency_style": "code",
            "currency_in_header": False}  # "USD 1,234"
MONEY = {"number_style": "decimal", "decimals": 2}
KPI = {"scalar.compact_primary_number": False}

# Per-currency tables: same four columns for spend and tax.
CURRENCY_COLUMNS = {
    col("currency"): {"column_title": "Currency"},
    col("invoices"): {"column_title": "Invoices"},
    col("amount_original"): {**MONEY, "column_title": "Amount (original)"},
    col("amount_usd"): {**USD, "decimals": 2, "column_title": "Amount (USD)"},
}

# Review queue: plain-English issue label per failed rule; Details = the validator's reason.
REVIEW_SQL = """with items as (
    select
        q.*,
        split_part(r.item, ': ', 1)                                  as rule,
        substr(r.item, length(split_part(r.item, ': ', 1)) + 3)       as detail,
        r.ord
    from marts.mart_review_queue q
    cross join lateral unnest(string_to_array(q.reasons, '; ')) with ordinality as r(item, ord)
),
labelled as (
    select
        *,
        case rule
            when 'duplicate'       then 'Duplicate invoice number'
            when 'total'           then 'Total doesn''t add up'
            when 'tax'             then 'Tax amount wrong'
            when 'subtotal'        then 'Subtotal doesn''t add up'
            when 'line_totals'     then 'Line total wrong'
            when 'required_fields' then 'Missing ' || replace(replace(detail, 'missing: ', ''), '_', ' ')
            when 'due_date'        then 'Due date before invoice date'
            when 'future_date'     then 'Invoice date in the future'
            when 'date_sequence'   then 'Invoice date out of sequence'
            else rule
        end as issue
    from items
)
select
    string_agg(issue, ', ' order by ord)                as issue,
    vendor_name                                         as vendor,
    invoice_number                                      as invoice,
    to_char(invoice_date, 'YYYY-MM-DD')                 as invoice_date,
    to_char(amount_at_stake, 'FM999,999,999,990.00') || ' ' || currency as amount,
    amount_at_stake_usd                                 as amount_usd,
    string_agg(detail, '; ' order by ord)               as details,
    doc_id
from labelled
group by doc_id, vendor_name, invoice_number, invoice_date, amount_at_stake, currency, amount_at_stake_usd
order by min(issue), amount_at_stake_usd desc"""

CARDS = [
    # ---- KPI row ---------------------------------------------------------
    dict(
        key="kpi_invoices", name="Invoices", aliases=["Invoices processed"], display="scalar",
        description="Invoices read by the pipeline (one per invoice: the scan where one exists, else the PDF).",
        sql="select count(*) as invoices from marts.fct_invoices",
        viz=KPI,
    ),
    dict(
        key="kpi_approved", name="Auto-approved", display="scalar",
        description="Invoices that passed every validation rule. The share is shown in the dashboard title "
                    "of this card and refreshed each time the build script runs.",
        sql="select count(*) filter (where is_approved) as auto_approved from marts.fct_invoices",
        viz=KPI,
    ),
    dict(
        key="kpi_spend", name="Approved spend", display="scalar",
        description=f"Total of approved invoices incl. tax, in USD. {FX_NOTE}",
        sql="select coalesce(sum(spend_usd), 0) as approved_spend_usd from marts.mart_spend_monthly",
        viz={**KPI, "column_settings": {col("approved_spend_usd"): {**USD_CODE, "decimals": 0}}},
    ),
    dict(
        key="review_count", name="In review", aliases=["Open review items"], display="scalar",
        description="Invoices that failed at least one validation rule and wait for a human reviewer.",
        sql="select count(*) as open_items from marts.mart_review_queue",
        viz=KPI,
    ),
    dict(
        key="review_amount", name="At stake", aliases=["At stake in review", "Amount at stake in review (USD)"],
        display="scalar",
        description=f"Total of the invoices waiting for review, in USD. {FX_NOTE}",
        sql="select coalesce(sum(amount_at_stake_usd), 0) as amount_at_stake_usd from marts.mart_review_queue",
        viz={**KPI, "column_settings": {col("amount_at_stake_usd"): {**USD_CODE, "decimals": 0}}},
    ),
    # ---- Currency tables -------------------------------------------------
    dict(
        key="spend_currency", name="Spend by currency", aliases=["Spend by currency (original currency)"],
        display="table",
        description="Approved invoices only, in the currency printed on the invoice, with the USD "
                    f"equivalent for comparison. {FX_NOTE}",
        sql="""select
    currency,
    sum(n_invoices)     as invoices,
    sum(spend_original) as amount_original,
    sum(spend_usd)      as amount_usd
from marts.mart_spend_monthly
group by currency
order by amount_usd desc""",
        viz={"column_settings": CURRENCY_COLUMNS},
    ),
    dict(
        key="tax_currency", name="Tax by currency", aliases=["Tax collected by currency"], display="table",
        description="Tax (VAT) charged on approved invoices, in the invoice currency, with the USD "
                    f"equivalent. {FX_NOTE}",
        sql="""select
    currency,
    sum(n_invoices)   as invoices,
    sum(tax_original) as amount_original,
    sum(tax_usd)      as amount_usd
from marts.mart_spend_monthly
group by currency
order by amount_usd desc""",
        viz={"column_settings": CURRENCY_COLUMNS},
    ),
    # ---- Charts ----------------------------------------------------------
    dict(
        key="spend_month", name="Approved spend by month (USD)", display="bar",
        description=f"Sum of invoice totals (incl. tax) of approved invoices, by invoice month. {FX_NOTE}",
        sql="""select to_char(invoice_month, 'YYYY-MM') as month, sum(spend_usd) as spend_usd,
       sum(n_invoices) as invoices
from marts.mart_spend_monthly
group by invoice_month
order by invoice_month""",
        viz={
            "graph.dimensions": ["month"],
            "graph.metrics": ["spend_usd"],
            "graph.tooltip_columns": [col("invoices")],
            "graph.x_axis.title_text": "Invoice month",
            "graph.y_axis.title_text": "Approved spend (USD)",
            "series_settings": {"spend_usd": {"color": BAR_COLOR}},
            "column_settings": {col("spend_usd"): {**USD, "decimals": 0, "column_title": "Spend (USD)"},
                                col("month"): {"column_title": "Month"}},
        },
    ),
    dict(
        key="top_vendors", name="Top 10 vendors by approved spend (USD)", display="row",
        description="Approved invoices only; each vendor under one canonical name, even when invoices "
                    f"print it differently (e.g. in capitals). {FX_NOTE}",
        sql="""select vendor_name as vendor, sum(spend_usd) as spend_usd
from marts.mart_spend_monthly
group by vendor_name
order by spend_usd desc
limit 10""",
        viz={
            "graph.dimensions": ["vendor"],
            "graph.metrics": ["spend_usd"],
            "graph.show_values": True,
            "graph.x_axis.labels_enabled": False,          # no "vendor" axis label
            "graph.y_axis.labels_enabled": True,
            "graph.y_axis.title_text": "Approved spend (USD)",
            "series_settings": {"spend_usd": {"color": BAR_COLOR}},
            "column_settings": {col("spend_usd"): {**USD, "decimals": 0, "column_title": "Approved spend (USD)"}},
        },
    ),
    # ---- Review queue ----------------------------------------------------
    dict(
        key="review_table", name="Review queue", display="table",
        description="Invoices waiting for a human reviewer: what is wrong, and the full reason from the "
                    "validation rule.",
        sql=REVIEW_SQL,
        viz={
            "table.columns": [
                {"name": n, "enabled": n != "doc_id"}
                for n in ("issue", "vendor", "invoice", "invoice_date", "amount", "amount_usd",
                          "details", "doc_id")
            ],
            # Pixel widths of the visible columns, in order: narrow fixed columns, Details gets the rest.
            "table.column_widths": [190, 200, 125, 105, 130, 115, 430],
            "column_settings": {
                col("issue"): {"column_title": "Issue"},
                col("vendor"): {"column_title": "Vendor"},
                col("invoice"): {"column_title": "Invoice"},
                col("invoice_date"): {"column_title": "Invoice date"},
                col("amount"): {"column_title": "Amount"},
                col("amount_usd"): {**USD, "decimals": 2, "column_title": "Amount (USD)"},
                col("details"): {"column_title": "Details", "text_wrapping": True},
                col("doc_id"): {"column_title": "Doc"},
            },
        },
    ),
]

INTRO_TEXT = ("**Synthetic data:** every vendor and amount is fictional. "
              "Spend = approved invoices, incl. tax.  \n"
              "USD at fixed illustrative rates (as of 2025-12-31), not market rates.")
TEXT_KEY = "intro_text"

# Grid is 24 columns wide. (card key, row, col, width, height)
LAYOUT = [
    (TEXT_KEY, 0, 0, 24, 2),
    # 5 KPIs over 24 columns; the longest title ("Auto-approved · 90%") gets the widest card.
    # Height 4 leaves room for a title to wrap instead of being cut off.
    ("kpi_invoices", 2, 0, 4, 4),
    ("kpi_approved", 2, 4, 6, 4),
    ("kpi_spend", 2, 10, 5, 4),
    ("review_count", 2, 15, 4, 4),
    ("review_amount", 2, 19, 5, 4),
    # 5 currency rows + header with no scrollbar.
    ("spend_currency", 6, 0, 12, 7),
    ("tax_currency", 6, 12, 12, 7),
    ("spend_month", 13, 0, 24, 7),
    ("top_vendors", 20, 0, 24, 8),
    # 12 review rows (Details may wrap to 2 lines) + header with no scrollbar.
    ("review_table", 28, 0, 24, 18),
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
        # Match by current name first, then by old names (renamed cards).
        match = next((n for n in [card["name"], *card.get("aliases", [])] if n in existing), None)
        if match:
            cid = existing[match]
            mb.put(f"/card/{cid}", json=body)
            renamed = f"  (was '{match}')" if match != card["name"] else ""
            print(f"  updated card {cid:>4}  {card['name']}{renamed}")
        else:
            cid = mb.post("/card", json=body)["id"]
            print(f"  created card {cid:>4}  {card['name']}")
        ids[card["key"]] = cid
    return ids


def approved_title(mb: Metabase, card_ids: dict[str, int]) -> str:
    """'Auto-approved · 90%': the share is computed from live data each time the script runs."""
    approved = mb.post(f"/card/{card_ids['kpi_approved']}/query", json={})["data"]["rows"][0][0]
    total = mb.post(f"/card/{card_ids['kpi_invoices']}/query", json={})["data"]["rows"][0][0]
    return f"Auto-approved · {approved / total:.0%}" if total else "Auto-approved"


def upsert_dashboard(mb: Metabase, coll_id: int, card_ids: dict[str, int],
                     titles: dict[str, str] | None = None) -> int:
    """titles: per-card dashboard title overrides (card key -> title)."""
    titles = titles or {}
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
            "visualization_settings": {"card.title": titles[key]} if key in titles else {},
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
        cid = dc["card_id"]
        name = dc.get("visualization_settings", {}).get("card.title") or dc["card"]["name"]
        res = mb.post(f"/dashboard/{dash_id}/dashcard/{dc['id']}/card/{cid}/query", json={"parameters": []})
        if res.get("status") != "completed" or res.get("error"):
            ok = False
            print(f"  ! {name}: {res.get('error')}")
            continue
        rows = res["data"]["rows"]
        print(f"  {name:<40} {res['row_count']:>4} rows   first: {rows[0] if rows else None}")
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
    dash_id = upsert_dashboard(mb, coll_id, card_ids, {"kpi_approved": approved_title(mb, card_ids)})

    print("5) Check")
    ok = check_dashboard(mb, dash_id)
    print(f"\nDashboard: {mb_url}/dashboard/{dash_id}")
    if not ok:
        sys.exit("ERROR: at least one card failed.")


if __name__ == "__main__":
    main()
