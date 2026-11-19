#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
if [[ "${1:-}" == --help || "${1:-}" == -h ]]; then
  echo 'Uso: sudo bash install.sh [--offline | --install-dependencies] [--env ARCHIVO] [--site YAML] [--token-file ARCHIVO] [--plc-username-file ARCHIVO] [--plc-password-file ARCHIVO]'
  exit 0
fi
[[ "$EUID" == 0 ]] || fail 'Ejecute sudo bash install.sh ...'
# shellcheck disable=SC1091
source /etc/os-release
[[ "$ID" == ubuntu && "$VERSION_ID" == 24.04 ]] || fail 'Requiere Ubuntu Desktop 24.04 LTS.'
[[ "$(uname -m)" == x86_64 ]] || fail 'Primera versión: solo linux/amd64.'
offline=0 install_deps=0 configure_args=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --offline) offline=1; shift;;
    --install-dependencies) install_deps=1; shift;;
    --site|--env|--token-file|--plc-username-file|--plc-password-file)
      [[ $# -ge 2 ]] || fail "Falta valor para $1"
      configure_args+=("$1" "$2"); shift 2;;
    --replace-config) configure_args+=("$1"); shift;;
    *) fail "Opción desconocida: $1";;
  esac
done
if ! command -v docker >/dev/null || ! docker compose version >/dev/null 2>&1; then
  [[ "$install_deps" == 1 && "$offline" == 0 ]] || fail 'Falta Docker/Compose. Instale previamente o autorice con --install-dependencies.'
  # Do not replace existing docker.io/containerd installations automatically.
  for package in docker.io docker-compose docker-compose-v2 podman-docker containerd runc; do
    if dpkg-query -W -f='${Status}' "$package" 2>/dev/null | grep -q 'install ok installed'; then
      fail "Conflicto $package: siga la migración oficial de Docker antes de continuar."
    fi
  done
  apt-get update
  apt-get install -y ca-certificates curl python3
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  cat > /etc/apt/sources.list.d/docker.sources <<EOF
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
EOF
  apt-get update
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
fi
command -v python3 >/dev/null || fail 'Falta python3 para configuración; instálelo previamente.'
systemctl enable --now docker.service containerd.service
require_docker
reject_legacy
# Pull before validation/configure (the latter never pulls silently).
if [[ ! -f .env ]]; then
  candidate="$TASK_ROOT/.env.example"
  for ((i=0; i<${#configure_args[@]}; i++)); do
    if [[ "${configure_args[i]}" == --env ]]; then candidate="${configure_args[i+1]}"; fi
  done
  install -m 0600 "$candidate" .env
fi
if [[ "$offline" == 0 ]]; then dc pull; else check_images; fi
filtered_args=()
for ((i=0; i<${#configure_args[@]}; i++)); do
  if [[ "${configure_args[i]}" == --env ]]; then ((i+=1)); continue; fi
  filtered_args+=("${configure_args[i]}")
done
bash "$TASK_ROOT/configure.sh" "${filtered_args[@]}"
bash "$TASK_ROOT/start.sh"
if ! bash "$TASK_ROOT/diagnose.sh"; then
  echo 'Servicios iniciados, pero PLC o central no disponibles. Revisar diagnóstico; OFFLINE puede ser esperado.' >&2
  exit 3
fi
echo 'Instalación verificada: servicios saludables y conectividad TCP/HTTP disponible.'
