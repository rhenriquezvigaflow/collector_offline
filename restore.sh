#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
backup="${1:?Uso: restore.sh /ruta/backup.dump --into-empty-database}"
[[ "${2:-}" == --into-empty-database ]] || fail 'Requiere --into-empty-database. Nunca sobrescribe una BD con tablas.'
[[ -f "$backup" ]] || fail 'Respaldo no encontrado.'
(cd "$(dirname "$backup")" && sha256sum --check "$(basename "$backup").sha256")
dc stop collector
dc up -d --no-build --pull never --wait postgres
tables="$(dc exec -T postgres sh -c 'psql -U postgres -d "$POSTGRES_DB" -Atc "SELECT count(*) FROM pg_tables WHERE schemaname = '\''public'\''"')"
[[ "$tables" == 0 ]] || fail 'Base no vacía. Restaure en otra instalación/volumen, no sobre históricos.'
dc exec -T postgres pg_restore --list < "$backup" >/dev/null
dc exec -T postgres sh -c 'pg_restore -U postgres -d "$POSTGRES_DB" --role="$APP_DB_USER" --no-owner --no-acl --exit-on-error --single-transaction' < "$backup"
echo 'Restauración completada en transacción. Revise identidad/configuración y ejecute start.sh.'
