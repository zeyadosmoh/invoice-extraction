-- Approved invoices only. Grain: month x vendor x currency. USD via fixed illustrative rates.
select
    invoice_month || '|' || vendor_id || '|' || currency as spend_key,
    invoice_month,
    vendor_id,
    vendor_name,
    currency,
    count(*)            as n_invoices,
    sum(total)          as spend_original,
    sum(tax_amount)     as tax_original,
    sum(total_usd)      as spend_usd,
    sum(tax_amount_usd) as tax_usd
from {{ ref('fct_invoices') }}
where is_approved
group by invoice_month, vendor_id, vendor_name, currency
