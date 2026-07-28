#!/usr/bin/env bash
set -Eeuo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DB_NAME="crystal_offline"
DB_ROLE="collector_local"
SQL_FILE="${PROJECT_DIR}/migrations/local/001_initial.sql"
MIGRATION_VERSION="001_initial.sql"

usage() {
  cat <<'EOF'
Uso:
  sudo bash scripts/apply-initial-schema.sh [opciones]

Opciones:
  --database NOMBRE   Base PostgreSQL (default: crystal_offline)
  --role NOMBRE       Rol propietario (default: collector_local)
  --sql RUTA          Ruta de 001_initial.sql
  -h, --help          Mostrar esta ayuda
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --database)
      DB_NAME="${2:?Falta el valor de --database}"
      shift 2
      ;;
    --role)
      DB_ROLE="${2:?Falta el valor de --role}"
      shift 2
      ;;
    --sql)
      SQL_FILE="${2:?Falta el valor de --sql}"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Opción desconocida: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "${EUID}" -ne 0 ]]; then
  echo "Este script requiere root. Ejecute: sudo bash $0 ..." >&2
  exit 77
fi
if [[ ! "${DB_NAME}" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  echo "Nombre de base inválido: ${DB_NAME}" >&2
  exit 2
fi
if [[ ! "${DB_ROLE}" =~ ^[A-Za-z_][A-Za-z0-9_-]*$ ]]; then
  echo "Nombre de rol inválido: ${DB_ROLE}" >&2
  exit 2
fi
if [[ ! -f "${SQL_FILE}" ]]; then
  echo "No se encontró la migración SQL: ${SQL_FILE}" >&2
  exit 2
fi

for command_name in psql pg_isready runuser; do
  if ! command -v "${command_name}" >/dev/null 2>&1; then
    echo "Falta el comando requerido: ${command_name}" >&2
    exit 69
  fi
done
if ! pg_isready --quiet; then
  echo "PostgreSQL no está disponible. Revise: systemctl status postgresql" >&2
  exit 69
fi

PSQL=(runuser -u postgres -- psql -X --set=ON_ERROR_STOP=1)

"${PSQL[@]}" \
  --set=db_name="${DB_NAME}" \
  --set=db_role="${DB_ROLE}" \
  postgres <<'SQL'
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

{
  printf 'BEGIN;\n'
  printf 'SET ROLE "%s";\n' "${DB_ROLE}"
  printf '%s\n' \
    'CREATE TABLE IF NOT EXISTS edge_schema_migration (' \
    '    version varchar(255) PRIMARY KEY,' \
    '    applied_at timestamptz NOT NULL DEFAULT now()' \
    ');'
  cat "${SQL_FILE}"
  printf '\n%s\n' \
    "INSERT INTO edge_schema_migration(version)" \
    "VALUES ('${MIGRATION_VERSION}')" \
    'ON CONFLICT (version) DO NOTHING;' \
    'COMMIT;'
} | "${PSQL[@]}" "${DB_NAME}"

EXPECTED_TABLE_COUNT="$(
  "${PSQL[@]}" --tuples-only --no-align "${DB_NAME}" <<'SQL'
SELECT count(*)
FROM pg_catalog.pg_tables
WHERE schemaname = 'public'
  AND tablename IN (
    'local_outage_session',
    'edge_runtime_state',
    'local_scada_minute',
    'local_scada_event',
    'edge_tag_state'
  );
SQL
)"
EXPECTED_TABLE_COUNT="${EXPECTED_TABLE_COUNT//[[:space:]]/}"

if [[ "${EXPECTED_TABLE_COUNT}" != "5" ]]; then
  echo "Verificación fallida: se esperaban 5 tablas y hay ${EXPECTED_TABLE_COUNT}." >&2
  exit 1
fi

echo "OK: database=${DB_NAME}, migración=${MIGRATION_VERSION}, tablas=5."
