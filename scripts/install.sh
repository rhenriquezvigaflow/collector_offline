#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo ./scripts/install.sh" >&2
  exit 1
fi

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
APP_DIR="/opt/crystal/collector-offline"
CONFIG_DIR="/etc/crystal/collector-offline"
SERVICE_USER="collector_local"

apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv postgresql postgresql-client ca-certificates

if ! id "${SERVICE_USER}" >/dev/null 2>&1; then
  useradd --system --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${SERVICE_USER}"
fi

install -d -m 0750 -o "${SERVICE_USER}" -g "${SERVICE_USER}" "${APP_DIR}" "${CONFIG_DIR}"
for item in src migrations scripts systemd pyproject.toml requirements.txt README.md; do
  cp -a "${SOURCE_DIR}/${item}" "${APP_DIR}/"
done
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${APP_DIR}"

for config_file in "${SOURCE_DIR}"/config/*.yml; do
  config_name="$(basename "${config_file}")"
  if [[ ! -f "${CONFIG_DIR}/${config_name}" ]]; then
    install -m 0640 -o "${SERVICE_USER}" -g "${SERVICE_USER}" \
      "${config_file}" "${CONFIG_DIR}/${config_name}"
  fi
done
if [[ ! -f /etc/crystal/collector-offline.env ]]; then
  install -m 0600 -o root -g "${SERVICE_USER}" \
    "${SOURCE_DIR}/.env.example" /etc/crystal/collector-offline.env
fi

runuser -u "${SERVICE_USER}" -- python3 -m venv "${APP_DIR}/.venv"
runuser -u "${SERVICE_USER}" -- "${APP_DIR}/.venv/bin/pip" install --upgrade pip
runuser -u "${SERVICE_USER}" -- "${APP_DIR}/.venv/bin/pip" install "${APP_DIR}[plc]"

bash "${APP_DIR}/scripts/provision-postgres.sh"

set -a
# shellcheck disable=SC1091
source /etc/crystal/collector-offline.env
set +a
runuser --preserve-environment -u "${SERVICE_USER}" -- \
  "${APP_DIR}/.venv/bin/collector-offline" migrate \
  --config "${CONFIG_DIR}/collector-offline.yml"

install -m 0644 "${SOURCE_DIR}/systemd/collector-offline.service" \
  /etc/systemd/system/collector-offline.service
systemctl daemon-reload
systemctl enable collector-offline.service

echo "Installation complete. Replace COLLECTOR_OFFLINE_TOKEN in"
echo "/etc/crystal/collector-offline.env, provision the same token hash centrally, then run:"
echo "  sudo systemctl start collector-offline"
