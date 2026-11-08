-- One row per invoice, original currency plus USD (fixed illustrative rates, see seeds/fx_rates_usd.csv).
with inv as (
    select * from {{ ref('stg_invoices') }}
),
aliases as (
    select * from {{ ref('vendor_aliases') }}
),
fx as (
    select * from {{ ref('fx_rates_usd') }}
)
select
    inv.doc_id,
    inv.source_file,
    inv.file_type,
    a.vendor_id,
    a.canonical_name                         as vendor_name,
    inv.vendor_name_printed,
    inv.invoice_number,
    inv.invoice_date,
    inv.invoice_month,
    inv.due_date,
    inv.currency,
    inv.subtotal,
    inv.discount,
    inv.tax_rate,
    inv.tax_amount,
    inv.total,
    fx.usd_per_unit                          as fx_usd_per_unit,
    fx.as_of_date                            as fx_as_of_date,
    round(inv.subtotal   * fx.usd_per_unit, 2) as subtotal_usd,
    round(inv.tax_amount * fx.usd_per_unit, 2) as tax_amount_usd,
    round(inv.total      * fx.usd_per_unit, 2) as total_usd,
    inv.n_line_items,
    inv.status,
    inv.status = 'auto_approved'             as is_approved,
    inv.n_failed,
    inv.failed_reasons,
    inv.skipped_rules,
    inv.model,
    inv.prompt_version,
    inv.loaded_at
from inv
left join aliases a on a.printed_name = inv.vendor_name_printed
left join fx on fx.currency = inv.currency
