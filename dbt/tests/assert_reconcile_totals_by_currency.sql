-- Reconciliation: per currency, invoice count and sum of totals in fct_invoices = raw.invoices.
with raw_totals as (
    select currency, count(*) as n, sum(total) as total
    from {{ source('raw', 'invoices') }}
    group by currency
),
fct_totals as (
    select currency, count(*) as n, sum(total) as total
    from {{ ref('fct_invoices') }}
    group by currency
)
select
    coalesce(r.currency, f.currency) as currency,
    r.n as raw_n, f.n as fct_n,
    r.total as raw_total, f.total as fct_total
from raw_totals r
full outer join fct_totals f on f.currency = r.currency
where r.n is distinct from f.n
   or r.total is distinct from f.total
