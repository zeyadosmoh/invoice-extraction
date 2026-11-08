select
    l.line_id,
    l.doc_id,
    f.vendor_id,
    f.invoice_date,
    f.invoice_month,
    f.currency,
    f.status,
    l.line_no,
    l.description,
    l.quantity,
    l.unit_price,
    l.line_total,
    round(l.line_total * f.fx_usd_per_unit, 2) as line_total_usd
from {{ ref('stg_invoice_lines') }} l
join {{ ref('fct_invoices') }} f using (doc_id)
