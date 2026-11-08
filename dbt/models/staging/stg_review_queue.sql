select
    doc_id || ':' || rule as review_item_id,
    doc_id,
    rule,
    reason
from {{ source('raw', 'review_queue') }}
