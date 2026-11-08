-- Open review items: every invoice that failed at least one rule (nothing is resolved yet in this
-- project, so all of them are open). One row per invoice, all reasons combined.
with reasons as (
    select
        doc_id,
        count(*)                                                   as n_failed_rules,
        string_agg(rule, ', ' order by rule)                       as failed_rules,
        string_agg(rule || ': ' || reason, '; ' order by rule)     as reasons
    from {{ ref('stg_review_queue') }}
    group by doc_id
)
select
    f.doc_id,
    f.source_file,
    f.invoice_number,
    f.vendor_id,
    f.vendor_name,
    f.invoice_date,
    f.currency,
    f.total       as amount_at_stake,
    f.total_usd   as amount_at_stake_usd,
    r.n_failed_rules,
    r.failed_rules,
    r.reasons
from {{ ref('fct_invoices') }} f
join reasons r using (doc_id)
where f.status = 'needs_review'
