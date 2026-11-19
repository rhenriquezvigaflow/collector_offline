#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
image="${1:?Uso: update.sh ghcr.io/org/collector:VERSION [--offline]}"
[[ $# -le 2 && ( $# -eq 1 || "$2" == --offline ) ]] || fail 'Solo se admite IMAGE [--offline].'
[[ "$image" =~ ^[a-zA-Z0-9./_:@-]+$ && "$image" != *:latest && ( "$image" == *:* || "$image" == *@sha256:* ) ]] || fail 'Use una referencia versionada o digest.'
if [[ "${2:-}" != --offline ]]; then docker pull --platform linux/amd64 "$image"; fi
docker image inspect "$image" >/dev/null || fail 'Importe primero la imagen.'
stamp="$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 backups
cp .env "backups/env-$stamp"
dc stop collector
bash "$TASK_ROOT/backup.sh" "$TASK_ROOT/backups/pre-update-$stamp.dump"
python3 "$TASK_ROOT/docker/ops.py" set-image "$image"
if bash "$TASK_ROOT/start.sh"; then
  echo "Actualización verificada. Configuración previa: backups/env-$stamp"
else
  echo "Fallo al actualizar. Collector detenido; configuración previa: backups/env-$stamp" >&2
  dc stop collector
  echo 'No se revierte automáticamente una migración. Ver OPERACION_MANTENIMIENTO.md.' >&2
  exit 1
fi
