#!/usr/bin/env bash
set -Eeuo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/crystal/collector-offline"
CONFIG_DIR="/etc/crystal/collector-offline"
ENV_FILE="/etc/crystal/collector-offline.env"
SERVICE_USER="collector_local"
SITE_SOURCE=""
SERVICE_WAS_ACTIVE=false

usage() {
  cat <<'EOF'
Uso:
  sudo bash scripts/install.sh [--site RUTA]

Instala Collector Offline en Ubuntu 24.04. Una instalacion nueva requiere
--site con el YAML aprobado de la laguna.

Una instalación existente conserva /etc/crystal/collector-offline/site.yml.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --site)
      SITE_SOURCE="${2:?Falta el valor de --site}"
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
  echo "Ejecute como root: sudo bash scripts/install.sh" >&2
  exit 77
fi
if [[ -n "${SITE_SOURCE}" && ! -f "${SITE_SOURCE}" ]]; then
  echo "No se encontró el YAML del sitio: ${SITE_SOURCE}" >&2
  exit 2
fi
if [[ ! -f "${CONFIG_DIR}/site.yml" && -z "${SITE_SOURCE}" ]]; then
  echo "Instalacion nueva: indique el YAML aprobado con --site RUTA." >&2
  exit 2
fi

for required in src migrations scripts systemd docs pyproject.toml requirements.txt README.md .env.example; do
  if [[ ! -e "${SOURCE_DIR}/${required}" ]]; then
    echo "El paquete está incompleto; falta: ${SOURCE_DIR}/${required}" >&2
    exit 2
  fi
done

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv postgresql postgresql-client ca-certificates curl

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0750 -o root -g "${SERVICE_USER}" "${APP_DIR}" "${CONFIG_DIR}"
if systemctl is-active --quiet collector-offline.service; then
  SERVICE_WAS_ACTIVE=true
  systemctl stop collector-offline.service
fi
for item in src migrations scripts systemd docs pyproject.toml requirements.txt README.md; do
  cp -a "${SOURCE_DIR}/${item}" "${APP_DIR}/"
done
chown -R root:"${SERVICE_USER}" "${APP_DIR}"
chmod -R g-w,o-rwx "${APP_DIR}"

if [[ ! -f "${CONFIG_DIR}/site.yml" ]]; then
  install -m 0640 -o root -g "${SERVICE_USER}" \
    "${SITE_SOURCE}" "${CONFIG_DIR}/site.yml"
  echo "YAML instalado en ${CONFIG_DIR}/site.yml."
else
  echo "Se conserva el YAML existente: ${CONFIG_DIR}/site.yml."
fi

install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
  /var/lib/crystal-offline \
  /var/lib/crystal-offline/outbox
for state_dir in creating pending uploading verified failed quarantine; do
  install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
    "/var/lib/crystal-offline/outbox/${state_dir}"
done

if [[ ! -f "${ENV_FILE}" ]]; then
  install -m 0600 -o root -g "${SERVICE_USER}" \
    "${SOURCE_DIR}/.env.example" "${ENV_FILE}"
fi

python3 -m venv "${APP_DIR}/.venv"
"${APP_DIR}/.venv/bin/pip" install --upgrade pip
"${APP_DIR}/.venv/bin/pip" install "${APP_DIR}[plc]"
chown -R root:"${SERVICE_USER}" "${APP_DIR}/.venv"
chmod -R g-w,o-rwx "${APP_DIR}/.venv"

"${APP_DIR}/.venv/bin/collector-offline" validate \
  --config "${CONFIG_DIR}/site.yml"

bash "${APP_DIR}/scripts/provision-postgres.sh"

set -a
# shellcheck disable=SC1090
source "${ENV_FILE}"
set +a
runuser --preserve-environment -u "${SERVICE_USER}" -- \
  "${APP_DIR}/.venv/bin/collector-offline" migrate \
  --config "${CONFIG_DIR}/site.yml"

install -m 0644 "${SOURCE_DIR}/systemd/collector-offline.service" \
  /etc/systemd/system/collector-offline.service
systemctl daemon-reload
systemctl enable collector-offline.service
if [[ "${SERVICE_WAS_ACTIVE}" == true ]]; then
  systemctl restart collector-offline.service
  systemctl --no-pager --full status collector-offline.service
fi

cat <<EOF
Instalación completada.

1. Configure el token del nodo en ${ENV_FILE}.
2. Registre el mismo source_node_id y hash del token en el backend central.
3. Inicie y verifique (en una actualizacion que ya estaba activa, el instalador
   la reinicia automaticamente):
   sudo systemctl restart collector-offline
   sudo systemctl status collector-offline --no-pager
   sudo journalctl -u collector-offline -f -o cat

En el futuro, la configuración del sitio se cambia solamente en:
  ${CONFIG_DIR}/site.yml
EOF
