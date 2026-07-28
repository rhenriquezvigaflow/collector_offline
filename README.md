# Collector Offline

Nodo local de contingencia SCADA para Ubuntu Server 24.04. El Collector Central
sigue siendo la fuente principal: este servicio solo lee PLC durante OFFLINE,
conserva lecturas en PostgreSQL local y transfiere el historico como chunks
CSV.gz reanudables cuando entra en SYNCING.

La captura PLC, drivers Rockwell/Siemens, transformacion, escritura local y
frecuencia de 60 segundos no cambian. El backfill JSON fue retirado.

## Arquitectura

~~~mermaid
flowchart LR
    PLC[PLC Siemens / Rockwell] -->|cada 60 s, solo OFFLINE| CAP[Captura local]
    CAP --> LDB[(PostgreSQL crystal_offline)]
    LDB --> BUILD[Chunk Builder]
    BUILD --> OUT[/var/lib/crystal-offline/outbox]
    OUT -->|partes HTTPS reanudables| API[API central /offline-transfer]
    API --> WORK[Validacion e import worker]
    WORK --> CDB[(scada_minute / scada_event)]
    API -->|VERIFIED| OUT
    SUP[Supervisor ONLINE/OFFLINE/SYNCING] --> CAP
~~~

El builder y uploader viven en un thread separado. Gzip, fsync y HTTP nunca
bloquean el loop de captura. Hay un chunk y una parte activos. PostgreSQL
mantiene locks, leases, asociacion fila/chunk y progreso confirmado.

~~~text
CREATING -> SEALED -> PENDING -> UPLOADING
UPLOADING -> PENDING | UPLOADED | IMPORTING | VERIFIED | QUARANTINED
UPLOADED -> IMPORTING | VERIFIED | QUARANTINED
IMPORTING -> VERIFIED | QUARANTINED
~~~

Un FAILED del import central vuelve a ejecutar complete con backoff; no se pierde
el chunk. Solo VERIFIED marca filas sent. Los archivos VERIFIED se retienen siete
dias y su metadata, noventa dias por defecto.

## Source obligatorio

Cada fila local, CSV, manifiesto e import central usa exactamente:

~~~text
source = collector_offline
~~~

source_node_id y lagoon_id son campos separados y nunca sustituyen source.

## Configuracion de sitio

El repositorio no incluye un config/site.yml de produccion. Para una
instalacion nueva se entrega explicitamente el YAML aprobado con --site. El
instalador lo copia a /etc/crystal/collector-offline/site.yml y nunca lo
sobrescribe durante upgrades.

El mismo YAML de PLC/tags existente sigue siendo valido; agregue solo el bloque
offline:

~~~yaml
lagoon_id: "laguna_norte"
product_type: "crystal"
source: rockwell
rockwell:
  ip: "192.168.20.10"
  slot: 0
tags:
  Motor: "Motor"

offline:
  central:
    base_url: "https://backend-central.example.com/api"
~~~

base_url deriva:

- https://backend-central.example.com/api/health/ready
- https://backend-central.example.com/api/offline-transfer

HTTP privado solo se admite como excepcion explicita de transicion mediante
allow_private_http: true. Produccion debe usar HTTPS y validacion TLS.

El formato legacy config/collector-offline.yml sigue siendo legible. Debe usar
central.transfer_url; sync_url solo se acepta para derivar la URL nueva durante
una actualizacion y nunca recibe datos.

Los limites edge estan alineados con el central: 20.000 filas, 25 MiB sin
comprimir y partes de hasta 2 MiB. Defaults:

~~~yaml
chunk:
  outbox_path: "/var/lib/crystal-offline/outbox"
  max_rows: 20000
  max_uncompressed_size_mb: 25
  max_age_minutes: 360
  claim_timeout_seconds: 900
transfer:
  part_size_kb: 1024
  max_parallel_uploads: 1
  connect_timeout_seconds: 10
  request_timeout_seconds: 60
  retry_initial_seconds: 15
  retry_max_seconds: 300
retention:
  verified_files_days: 7
  verified_metadata_days: 90
~~~

## Instalacion

Fresh install:

~~~bash
sudo bash scripts/install.sh --site /ruta/al/site.yml
sudoedit /etc/crystal/collector-offline.env
sudo systemctl start collector-offline
sudo systemctl status collector-offline --no-pager
~~~

Upgrade con site.yml ya instalado:

~~~bash
sudo bash scripts/install.sh
~~~

El instalador detiene un servicio activo antes de copiar y migrar, conserva el
YAML, crea el StateDirectory/outbox con owner collector_local y reinicia si el
servicio estaba activo.

En el backend central:

~~~bash
psql "$CENTRAL_DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f scripts/create_offline_transfer.sql
~~~

Registre source_node_id, lagoon_id y el hash SHA-256 del token en
offline_collector_node. El token plano solo vive en
/etc/crystal/collector-offline.env, modo 0600.

## API central

~~~text
POST /offline-transfer/transfers
GET  /offline-transfer/transfers/{transfer_id}
PUT  /offline-transfer/transfers/{transfer_id}/parts/{part_number}
POST /offline-transfer/transfers/{transfer_id}/complete
~~~

Todas usan Bearer token. Las partes son application/octet-stream con
Content-Length, SHA-256 y offset opcional. Crear/repetir transferencias y partes identicas es
idempotente.

## Operacion

~~~bash
curl -s http://127.0.0.1:9187/health/live
curl -s http://127.0.0.1:9187/status
sudo -u collector_local -H /opt/crystal/collector-offline/.venv/bin/collector-offline \
  status --config /etc/crystal/collector-offline/site.yml
sudo journalctl -u collector-offline -f -o cat
~~~

El status incluye backlog de filas, chunks por estado y oldest_pending_at. Los
logs incluyen creacion/compresion, partes, bytes, velocidad, reintentos,
progreso central, VERIFIED y cuarentena, sin tokens ni payloads completos.

La estructura del outbox es:

~~~text
creating/ pending/ uploading/ verified/ failed/ quarantine/
~~~

## Desarrollo

~~~bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e '.[plc,dev]'
pytest -q
ruff check src tests
mypy src/collector_offline
~~~

Documentacion:

- docs/OFFLINE_TRANSFER.md: contrato, estados, recovery, instalacion y rollback.
- docs/MAPPING.md: columnas CSV hacia scada_minute/scada_event.
- docs/MIGRACION_VM_UBUNTU.md: despliegue, backup y recuperacion.
- docs/REUTILIZACION_NUEVA_LAGUNA.md: reutilizacion del YAML PLC existente.
