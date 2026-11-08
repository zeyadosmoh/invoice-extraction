-- One row per real vendor (ground-truth vendor_id), with its canonical display name.
with aliases as (
    select * from {{ ref('vendor_aliases') }}
),
vendors as (
    select
        vendor_id,
        min(canonical_name)         as canonical_name,
        count(*)                    as n_printed_variants,
        string_agg(printed_name, ' | ' order by printed_name) as printed_names
    from aliases
    group by vendor_id
),
invoice_stats as (
    select
        a.vendor_id,
        count(*)                as n_invoices,
        min(i.invoice_date)     as first_invoice_date,
        max(i.invoice_date)     as last_invoice_date
    from {{ ref('stg_invoices') }} i
    join aliases a on a.printed_name = i.vendor_name_printed
    group by a.vendor_id
)
select
    v.vendor_id,
    v.canonical_name,
    v.n_printed_variants,
    v.printed_names,
    coalesce(s.n_invoices, 0) as n_invoices,
    s.first_invoice_date,
    s.last_invoice_date
from vendors v
left join invoice_stats s using (vendor_id)
