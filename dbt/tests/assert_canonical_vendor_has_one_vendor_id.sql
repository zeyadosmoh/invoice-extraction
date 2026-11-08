-- Fails if the seed merges two different vendor_ids into one canonical name,
-- or gives one vendor_id two canonical names.
select 'canonical_name' as side, canonical_name as value, count(distinct vendor_id) as n
from {{ ref('vendor_aliases') }}
group by canonical_name
having count(distinct vendor_id) > 1
union all
select 'vendor_id', vendor_id, count(distinct canonical_name)
from {{ ref('vendor_aliases') }}
group by vendor_id
having count(distinct canonical_name) > 1
