#!/usr/bin/env bash
set -Eeuo pipefail
TASK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$TASK_ROOT"
umask 077
fail() { echo "ERROR: $*" >&2; exit 1; }
require_docker() {
  command -v docker >/dev/null || fail 'Falta Docker Engine. Ver INSTALACION_MINIPC.md.'
  docker compose version >/dev/null || fail 'Falta Docker Compose plugin.'
  docker info >/dev/null 2>&1 || fail 'Docker Engine no disponible o sin permisos.'
}
require_config() { [[ -f .env && -s runtime/config/site.yml ]] || fail 'Ejecute configure.sh primero.'; }
dc() { docker compose --env-file "$TASK_ROOT/.env" -f "$TASK_ROOT/compose.yml" "$@"; }
image_list() { dc config --images | sort -u; }
check_images() {
  local image
  while IFS= read -r image; do
    docker image inspect "$image" >/dev/null || fail "Imagen no importada: $image"
    [[ "$(docker image inspect --format '{{.Architecture}}' "$image")" == amd64 ]] || fail "Arquitectura incorrecta: $image"
  done < <(image_list)
}
reject_legacy() {
  if command -v systemctl >/dev/null && systemctl is-active --quiet collector-offline.service; then
    fail 'Collector systemd activo. Deténgalo en una ventana de migración antes de usar Docker.'
  fi
}
