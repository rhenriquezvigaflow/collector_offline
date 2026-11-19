#!/usr/bin/env bash
source "$(dirname "$0")/docker/common.sh"
require_docker
require_config
check_images
destination="${1:?Uso: export-usb.sh /ruta/paquete-NUEVO}"
[[ ! -e "$destination" ]] || fail 'El destino debe ser nuevo.'
mkdir -m 0700 -p -- "$destination"
destination="$(cd "$destination" && pwd)"
[[ "$destination" != "$TASK_ROOT" && "$destination" != "$TASK_ROOT/runtime"* ]] || fail 'Destino inválido.'
images=()
: > "$destination/images.txt"
for service in collector postgres; do
  original="$(dc config --format json | python3 -c 'import json,sys; print(json.load(sys.stdin)["services"][sys.argv[1]]["image"])' "$service")"
  image_id="$(docker image inspect --format '{{.Id}}' "$original")"
  alias="collector-offline-usb/$service:sha-${image_id#sha256:}"
  docker tag "$original" "$alias"
  images+=("$alias")
  printf '%s %s %s\n' "$alias" "$image_id" "$original" >> "$destination/images.txt"
done
docker save --output "$destination/images.tar" "${images[@]}"
python3 "$TASK_ROOT/docker/ops.py" usb-env "$destination/.env.usb" "${images[@]}"
# Explicit allowlist. No config/, runtime/, .git, SSH keys, .env or databases.
tar -czf "$destination/collector-distribution.tar.gz" \
  compose.yml .env.example site.example.yml .gitattributes \
  install.sh configure.sh start.sh stop.sh status.sh update.sh backup.sh restore.sh diagnose.sh \
  export-usb.sh import-usb.sh \
  docker/common.sh docker/ops.py docker/postgres-init.sh \
  ARQUITECTURA_DOCKER.md INSTALACION_MINIPC.md OPERACION_MANTENIMIENTO.md \
  docs/AUDITORIA_DOCKER.md docs/SEGURIDAD_DEPENDENCIAS.md docs/RESULTADOS_PRUEBAS.md \
  docs/dependency-audit.json \
  -C "$destination" .env.usb
(cd "$destination" && sha256sum images.tar collector-distribution.tar.gz images.txt .env.usb > SHA256SUMS)
echo "Paquete sin secretos: $destination. Transfiera configuración/token por canal separado."
