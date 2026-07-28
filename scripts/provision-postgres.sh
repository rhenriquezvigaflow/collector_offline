#!/usr/bin/env bash
set -euo pipefail

# Base separada para el buffer offline. La aplicación usa autenticación peer
# por socket local con el usuario Linux/PostgreSQL collector_local.
DB_NAME="crystal_offline"
DB_ROLE="collector_local"

if [[ "${EUID}" -eq 0 ]]; then
  PSQL=(runuser -u postgres -- psql)
else
  PSQL=(sudo -u postgres psql)
fi

"${PSQL[@]}" --set=ON_ERROR_STOP=1 \
  --set=db_name="${DB_NAME}" --set=db_role="${DB_ROLE}" postgres <<'SQL'
SELECT format('CREATE ROLE %I LOGIN', :'db_role')
WHERE NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :'db_role')
\gexec

SELECT format('CREATE DATABASE %I OWNER %I ENCODING %L', :'db_name', :'db_role', 'UTF8')
WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'db_name')
\gexec

SELECT format('ALTER DATABASE %I SET timezone TO %L', :'db_name', 'UTC')
\gexec

SELECT format('REVOKE CONNECT ON DATABASE %I FROM PUBLIC', :'db_name')
\gexec

SELECT format('GRANT CONNECT ON DATABASE %I TO %I', :'db_name', :'db_role')
\gexec
SQL

echo "PostgreSQL listo: database=${DB_NAME}, role=${DB_ROLE}."
