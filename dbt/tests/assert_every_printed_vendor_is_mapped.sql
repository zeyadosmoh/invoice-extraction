-- Fails if any printed vendor name has no entry in seeds/vendor_aliases.csv.
select distinct i.vendor_name_printed
from {{ ref('stg_invoices') }} i
left join {{ ref('vendor_aliases') }} a on a.printed_name = i.vendor_name_printed
where a.printed_name is null
