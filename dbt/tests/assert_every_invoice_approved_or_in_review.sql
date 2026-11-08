-- Reconciliation: every raw invoice is exactly one of: approved (in fct_invoices) or in the review queue.
with r as (
    select doc_id from {{ source('raw', 'invoices') }}
),
approved as (
    select doc_id from {{ ref('fct_invoices') }} where is_approved
),
review as (
    select doc_id from {{ ref('mart_review_queue') }}
)
select
    r.doc_id,
    (a.doc_id is not null) as is_approved,
    (q.doc_id is not null) as in_review
from r
left join approved a using (doc_id)
left join review q using (doc_id)
where (a.doc_id is null) = (q.doc_id is null)   -- neither, or both
