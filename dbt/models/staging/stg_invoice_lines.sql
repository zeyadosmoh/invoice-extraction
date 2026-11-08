select
    doc_id || '-' || lpad(line_no::text, 3, '0') as line_id,
    doc_id,
    line_no,
    description,
    quantity,
    unit_price,
    line_total
from {{ source('raw', 'invoice_lines') }}
