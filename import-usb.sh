#!/usr/bin/env bash
set -Eeuo pipefail
bundle="${1:?Uso: import-usb.sh /ruta/paquete /ruta/instalacion-NUEVA}"
target="${2:?Falta directorio nuevo de instalación}"
[[ ! -e "$target" ]] || { echo 'Destino debe ser nuevo para no sobrescribir configuración.' >&2; exit 1; }
bundle="$(cd "$bundle" && pwd)"
(cd "$bundle" && sha256sum --check SHA256SUMS)
command -v docker >/dev/null
docker info >/dev/null
docker load --input "$bundle/images.tar"
while read -r alias expected_id original; do
  [[ "$alias" =~ ^collector-offline-usb/(collector|postgres):sha-[a-f0-9]{64}$ ]] || exit 1
  [[ "$(docker image inspect --format '{{.Id}}' "$alias")" == "$expected_id" ]] || {
    echo "Imagen importada no coincide: $alias" >&2; exit 1;
  }
done < "$bundle/images.txt"
# Reject traversal and symlinks before extraction. Trust originates with a signed/offline
# release delivered by the administrator; SHA256 alone does not authenticate the sender.
python3 - "$bundle/collector-distribution.tar.gz" "$target" <<'PY'
import pathlib, sys, tarfile
archive, target = sys.argv[1:]
with tarfile.open(archive) as package:
    for item in package.getmembers():
        path = pathlib.PurePosixPath(item.name)
        if path.is_absolute() or '..' in path.parts or not (item.isfile() or item.isdir()):
            raise SystemExit('Unsafe distribution archive')
    pathlib.Path(target).mkdir(parents=True, mode=0o700)
    package.extractall(target, filter='data')
PY
echo "Importado en $target. Configure .env, YAML y token; ejecute install.sh --offline."
