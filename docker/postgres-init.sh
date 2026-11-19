#!/usr/bin/env bash
set -Eeuo pipefail
[[ "$APP_DB_USER" =~ ^[a-z_][a-z0-9_]{0,62}$ ]] || { echo 'APP_DB_USER inválido' >&2; exit 2; }
[[ "$APP_DB_USER" != postgres ]] || { echo 'La aplicación no puede usar postgres' >&2; exit 2; }
export APP_DB_PASSWORD
APP_DB_PASSWORD="$(cat "$APP_DB_PASSWORD_FILE")"
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=app_user="$APP_DB_USER" --set=app_db="$POSTGRES_DB" <<'SQL'
\getenv app_password APP_DB_PASSWORD
CREATE ROLE :"app_user" LOGIN PASSWORD :'app_password' NOSUPERUSER NOCREATEDB NOCREATEROLE;
ALTER DATABASE :"app_db" OWNER TO :"app_user";
REVOKE ALL ON DATABASE :"app_db" FROM PUBLIC;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE, CREATE ON SCHEMA public TO :"app_user";
ALTER DATABASE :"app_db" SET timezone TO 'UTC';
SQL
unset APP_DB_PASSWORD
