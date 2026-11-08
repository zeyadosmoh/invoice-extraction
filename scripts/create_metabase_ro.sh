#!/usr/bin/env bash
# Create (or update) the read-only Postgres role Metabase connects as.
# It can read the `marts` schema only. Idempotent. Password comes from .env (METABASE_RO_PASSWORD).
#
#   bash scripts/create_metabase_ro.sh
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; . ./.env; set +a
: "${METABASE_RO_PASSWORD:?set METABASE_RO_PASSWORD in .env}"

docker exec -i invoice_pg psql -q -U "$POSTGRES_USER" -d "$POSTGRES_DB" -v ON_ERROR_STOP=1 \
  -v pw="$METABASE_RO_PASSWORD" -v db="$POSTGRES_DB" -v owner="$POSTGRES_USER" <<'SQL'
SELECT 'CREATE ROLE metabase_ro LOGIN'
 WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'metabase_ro') \gexec
SELECT format('ALTER ROLE metabase_ro PASSWORD %L', :'pw') \gexec
CREATE SCHEMA IF NOT EXISTS marts;
GRANT CONNECT ON DATABASE :"db" TO metabase_ro;
GRANT USAGE ON SCHEMA marts TO metabase_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA marts TO metabase_ro;
ALTER DEFAULT PRIVILEGES FOR ROLE :"owner" IN SCHEMA marts GRANT SELECT ON TABLES TO metabase_ro;
SQL
echo "metabase_ro ready (SELECT on schema marts)."
