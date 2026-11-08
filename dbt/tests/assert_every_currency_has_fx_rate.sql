select distinct i.currency
from {{ ref('stg_invoices') }} i
left join {{ ref('fx_rates_usd') }} fx on fx.currency = i.currency
where fx.currency is null
