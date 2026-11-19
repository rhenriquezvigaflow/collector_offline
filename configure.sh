#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
[[ "$EUID" == 0 ]] || fail 'Ejecute con sudo para preparar permisos UID 10001.'
site='' env_file='' token_file='' username_file='' password_file='' replace=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site) site="${2:?}"; shift 2;;
    --env) env_file="${2:?}"; shift 2;;
    --token-file) token_file="${2:?}"; shift 2;;
    --plc-username-file) username_file="${2:?}"; shift 2;;
    --plc-password-file) password_file="${2:?}"; shift 2;;
    --replace-config) replace=1; shift;;
    *) fail "Opción desconocida: $1";;
  esac
done
install -d -m 0700 runtime runtime/secrets backups
install -d -m 0750 -o 10001 -g 10001 runtime/config
if [[ ! -f .env ]]; then
  [[ -n "$env_file" ]] || env_file="$TASK_ROOT/.env.example"
  install -m 0600 "$env_file" .env
elif [[ -n "$env_file" ]]; then
  fail '.env ya existe; edítelo explícitamente, no se sobrescribe.'
fi
require_docker
volume_name="$(dc config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["volumes"]["postgres_data"]["name"])')"
if docker volume inspect "$volume_name" >/dev/null 2>&1; then
  for secret in postgres_password database_password; do
    [[ -s "runtime/secrets/$secret" ]] || fail "Volumen existente pero falta $secret. Recupere el secreto; no se regenerará."
  done
fi
if [[ ! -s runtime/config/site.yml || "$replace" == 1 ]]; then
  [[ -n "$site" && -f "$site" ]] || fail 'Indique --site /ruta/aprobada/site.yml con tags reales.'
  # Preserve relative YAML includes. Never copy .env, keys or production databases.
  for yaml in "$(dirname "$site")"/*.yml "$(dirname "$site")"/*.yaml; do
    [[ -f "$yaml" ]] || continue
    install -m 0440 -o 10001 -g 10001 "$yaml" "runtime/config/$(basename "$yaml")"
  done
  install -m 0440 -o 10001 -g 10001 "$site" runtime/config/site.yml
fi
for secret in postgres_password database_password; do
  if [[ ! -s "runtime/secrets/$secret" ]]; then
    python3 -c 'import secrets; print(secrets.token_hex(32))' > "runtime/secrets/$secret"
  fi
done
if [[ -n "$token_file" ]]; then
  if [[ -s runtime/secrets/central_token ]]; then
    cmp -s "$token_file" runtime/secrets/central_token || fail 'Token distinto ya configurado; use el procedimiento de rotación.'
  else
    install -m 0400 "$token_file" runtime/secrets/central_token
  fi
fi
[[ -s runtime/secrets/central_token ]] || fail 'Indique --token-file con el token autorizado del nodo.'
[[ -z "$username_file" ]] || install -m 0400 "$username_file" runtime/secrets/plc_username
[[ -z "$password_file" ]] || install -m 0400 "$password_file" runtime/secrets/plc_password
touch runtime/secrets/plc_username runtime/secrets/plc_password
chown 10001:10001 runtime/secrets/{database_password,central_token,plc_username,plc_password}
chmod 0400 runtime/secrets/*
# postgres starts its official init scripts as its own UID; both DB passwords
# must be readable there. Directories remain root 0700 on the host.
chmod 0444 runtime/secrets/{postgres_password,database_password}
require_config
dc config --quiet
dc run --rm --no-deps --pull never collector validate
echo 'Configuración validada. No se inició captura ni se enviaron datos.'
