#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
install -d -m 0700 backups
backup="${1:-$TASK_ROOT/backups/offline-$(date -u +%Y%m%dT%H%M%SZ)-$$.dump}"
[[ ! -e "$backup" && ! -e "$backup.partial" ]] || fail 'El destino del respaldo ya existe.'
trap 'rm -f -- "$backup.partial"' EXIT
dc exec -T postgres sh -c 'pg_dump -U postgres -d "$POSTGRES_DB" --format=custom --no-owner --no-acl' > "$backup.partial"
dc exec -T postgres pg_restore --list < "$backup.partial" >/dev/null
mv -- "$backup.partial" "$backup"
(cd "$(dirname "$backup")" && sha256sum -- "$(basename "$backup")") > "$backup.sha256"
echo "$backup"
