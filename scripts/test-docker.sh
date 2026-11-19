#!/usr/bin/env bash
set -Eeuo pipefail
cd "$(dirname "$0")/.."
command -v docker >/dev/null
docker info >/dev/null
project="collector-test-$(date +%s)-$$"
[[ "$project" =~ ^collector-test-[0-9]+-[0-9]+$ ]]
export TEST_TLS_DIR
TEST_TLS_DIR="$(mktemp -d)"
chmod 0755 "$TEST_TLS_DIR"
openssl req -x509 -newkey rsa:2048 -nodes -days 1 -keyout "$TEST_TLS_DIR/central.key" \
  -out "$TEST_TLS_DIR/central.crt" -subj '/CN=central' \
  -addext 'subjectAltName=DNS:central' >/dev/null 2>&1
chmod 0444 "$TEST_TLS_DIR"/*
printf '%s' 'isolated-app-password' > "$TEST_TLS_DIR/database_password"
chmod 0444 "$TEST_TLS_DIR/database_password"
dc_test() { docker compose --project-name "$project" -f tests/docker/compose.yml "$@"; }
cleanup() {
  result=$?
  if [[ "$result" != 0 ]]; then dc_test logs --no-color || true; fi
  # The unique project only contains disposable test data, never the operational stack.
  dc_test down --volumes --remove-orphans
  rm -f -- "$TEST_TLS_DIR/central.key" "$TEST_TLS_DIR/central.crt" \
    "$TEST_TLS_DIR/database_password" "$TEST_TLS_DIR/backup.dump"
  rmdir -- "$TEST_TLS_DIR"
  exit "$result"
}
trap cleanup EXIT
docker image inspect collector-offline:test >/dev/null
dc_test up -d --wait --wait-timeout 120
dc_test run --rm tester outage
dc_test restart postgres collector
dc_test up -d --wait --wait-timeout 120
dc_test run --rm tester persisted
dc_test up -d --force-recreate --wait --wait-timeout 120 collector postgres
dc_test run --rm tester persisted
dc_test stop collector
dc_test exec -T postgres pg_dump -U postgres -d collector_test --format=custom \
  --no-owner --no-acl > "$TEST_TLS_DIR/backup.dump"
dc_test exec -T postgres createdb -U postgres -O collector_local restore_test
dc_test exec -T postgres pg_restore -U postgres -d restore_test --role=collector_local \
  --no-owner --no-acl --single-transaction --exit-on-error < "$TEST_TLS_DIR/backup.dump"
signature="SELECT (SELECT md5(string_agg(id::text||sync_status,',' ORDER BY id)) FROM local_scada_minute), (SELECT md5(string_agg(id::text||sync_status||is_open::text,',' ORDER BY id)) FROM local_scada_event), (SELECT state FROM edge_runtime_state WHERE id=1)"
original="$(dc_test exec -T postgres psql -U postgres -d collector_test -Atc "$signature")"
restored="$(dc_test exec -T postgres psql -U postgres -d restore_test -Atc "$signature")"
[[ "$original" == "$restored" ]]
dc_test up -d --wait collector
dc_test run --rm tester recover
# Verify Docker's restart policy rather than masking it with a manual compose start.
collector_id="$(dc_test ps -q collector)"
previous_restarts="$(docker inspect --format '{{.RestartCount}}' "$collector_id")"
docker kill --signal KILL "$collector_id" >/dev/null
for ((i=0; i<60; i++)); do
  restarts="$(docker inspect --format '{{.RestartCount}}' "$collector_id")"
  if [[ "$restarts" -gt "$previous_restarts" ]]; then break; fi
  sleep 1
done
[[ "$restarts" -gt "$previous_restarts" ]]
dc_test up -d --wait collector
dc_test run --rm tester recover
if [[ "${1:-}" == --volume-500k ]]; then
  available_kb="$(awk '/MemAvailable/ {print $2}' /proc/meminfo)"
  docker_root="$(docker info --format '{{.DockerRootDir}}')"
  [[ -d "$docker_root" ]] || { echo 'No se puede verificar disco del daemon remoto; no ejecutar volumen.' >&2; exit 1; }
  free_kb="$(df -Pk "$docker_root" | awk 'NR==2 {print $4}')"
  [[ "$available_kb" -ge 4194304 && "$free_kb" -ge 8388608 ]] || { echo 'Requiere 4 GiB RAM libre y 8 GiB disco libre.' >&2; exit 1; }
  dc_test stop collector
  dc_test run --rm tester volume
  dc_test up -d --wait collector
  started="$SECONDS"
  mkdir -p test-results
  stats_file="test-results/volume-$project.jsonl"
  dc_test run --rm tester volume-verify &
  verification_pid=$!
  while kill -0 "$verification_pid" 2>/dev/null; do
    docker stats --no-stream --format '{{json .}}' "$(dc_test ps -q collector)" \
      "$(dc_test ps -q postgres)" "$(dc_test ps -q central)" >> "$stats_file"
    sleep 2
  done
  wait "$verification_pid"
  echo "Transferencia 500k: $((SECONDS-started)) segundos"
  echo "Muestras CPU/RAM durante transferencia: $stats_file"
  docker stats --no-stream "$(dc_test ps -q collector)" "$(dc_test ps -q postgres)" "$(dc_test ps -q central)"
fi
echo 'PASS: initialization, role, readiness, outage, restart, recreation, leases, backup/restore, retries, duplicates, automatic restart and counts.'
