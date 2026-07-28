# Transferencia offline por chunks

El unico mecanismo de backfill activo convierte filas pendientes de PostgreSQL
local en archivos CSV UTF-8 comprimidos con gzip. La captura PLC, su intervalo
de 60 segundos, la transformacion y la escritura local no cambian.

## Flujo y concurrencia

1. El loop principal detecta ONLINE/OFFLINE/SYNCING y captura solo en OFFLINE.
2. Un thread independiente reclama filas con lock y lease durable.
3. Escribe CSV temporal, hace flush/fsync, comprime con gzip determinista,
   calcula SHA-256 y publica archivo/manifiesto mediante rename atomico.
4. En SYNCING mueve un chunk a uploading/, consulta progreso central y envia
   solo partes faltantes. Hay un chunk y una parte activos.
5. Una respuesta perdida es segura: el siguiente intento consulta las partes
   confirmadas y acepta already_received.
6. Solo VERIFIED marca las filas como sent y mueve archivos a verified/.

Gzip, fsync y HTTP se ejecutan fuera del hilo de captura. El uploader no opera
en ONLINE/OFFLINE; en OFFLINE el thread solo construye chunks.

## CSV y source

Cada transferencia contiene un solo data_kind.

Minute:

~~~text
lagoon_id,tag_id,bucket,state,value_num,value_bool,source
~~~

Event:

~~~text
id,lagoon_id,tag_id,tag_label,alert_type,previous_state,state,start_ts,end_ts,duration_sec,source
~~~

Los timestamps son UTC con sufijo Z, NULL es campo vacio, el orden es
timestamp/tag/id y source siempre es exactamente collector_offline.

## Outbox

La ruta predeterminada es /var/lib/crystal-offline/outbox:

~~~text
creating/ pending/ uploading/ verified/ failed/ quarantine/
~~~

systemd crea /var/lib/crystal-offline para collector_local y lo mantiene
escribible aun con ProtectSystem=strict. Al reiniciar, el servicio recupera
leases vencidos y reconcilia archivos si una caida ocurrio entre un rename y el
commit de paths en PostgreSQL.

## Configuracion

Estos defaults no modifican el bloque PLC/tags existente:

~~~yaml
offline:
  central:
    base_url: "https://central.example/api"
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
    poll_interval_seconds: 5
    lease_seconds: 180
  retention:
    verified_files_days: 7
    verified_metadata_days: 90
~~~

base_url deriva /health/ready y /offline-transfer. En el formato legacy tambien
se puede indicar central.transfer_url. sync_url se acepta solo como fallback de
compatibilidad para derivar el origen; nunca recibe datos.

## Contrato central

Todas las rutas usan Authorization: Bearer:

~~~text
POST /offline-transfer/transfers
GET  /offline-transfer/transfers/{transfer_id}
PUT  /offline-transfer/transfers/{transfer_id}/parts/{part_number}
POST /offline-transfer/transfers/{transfer_id}/complete
~~~

Las partes son application/octet-stream con Content-Length y X-Part-SHA256;
X-Part-Offset es opcional por compatibilidad. part_number comienza en 1. Crear la misma transferencia o
reenviar una parte identica es idempotente; metadata distinta para el mismo ID
es conflicto permanente.

El central responde estados UPLOADING, READY, IMPORTING, VERIFIED o FAILED,
ademas de received_parts/confirmed_parts y missing_parts. VERIFIED implica
reconstruccion, SHA-256, importacion, commit, conteo e idempotencia completados.
FAILED conserva el archivo y el collector vuelve a ejecutar complete con backoff.

## Estados locales

~~~text
CREATING -> SEALED -> PENDING -> UPLOADING
UPLOADING -> PENDING | UPLOADED | IMPORTING | VERIFIED | FAILED | QUARANTINED
UPLOADED -> IMPORTING | VERIFIED | FAILED | QUARANTINED
IMPORTING -> VERIFIED | FAILED | QUARANTINED
FAILED -> PENDING | QUARANTINED
~~~

Las transiciones no enumeradas son rechazadas. confirmed_parts se guarda en
PostgreSQL; el progreso no depende de memoria.

## Instalacion y actualizacion

Fresh install:

~~~bash
sudo bash scripts/install.sh --site /ruta/al/site.yml
sudoedit /etc/crystal/collector-offline.env
sudo systemctl start collector-offline
~~~

Upgrade con un site ya instalado:

~~~bash
sudo bash scripts/install.sh
~~~

El instalador conserva /etc/crystal/collector-offline/site.yml, detiene un
servicio activo antes de copiar/migrar y lo reinicia al finalizar.

Aplicacion manual de migraciones:

~~~bash
sudo systemctl stop collector-offline
sudo -u collector_local -H /opt/crystal/collector-offline/.venv/bin/collector-offline \
  migrate --config /etc/crystal/collector-offline/site.yml
sudo systemctl start collector-offline
~~~

## Verificacion

~~~bash
sudo -u collector_local -H /opt/crystal/collector-offline/.venv/bin/collector-offline \
  status --config /etc/crystal/collector-offline/site.yml
curl -s http://127.0.0.1:9187/status
sudo journalctl -u collector-offline -f -o cat
~~~

El status incluye conteos por estado de chunk y oldest_pending_at.

~~~bash
python -m pytest -q
python -m ruff check src tests
python -m mypy src/collector_offline
~~~

## Backup y rollback

Antes de actualizar, detener el servicio y respaldar juntos la base, outbox,
configuracion, aplicacion y unit. No restaurar una base antigua sobre un
backlog mas reciente.

Un rollback de codigo solo es seguro si la version anterior conoce la
migracion de chunks. Para volver temporalmente a una version sin chunks,
primero detener tambien los imports centrales y, dentro de una transaccion,
liberar solo filas syncing/chunked de chunks no VERIFIED a pending, poniendo
chunk_id, leases y retry en NULL. Conservar el outbox para auditoria y marcar
esos chunks QUARANTINED; nunca tocar filas de chunks VERIFIED.

La operacion puede duplicar intentos ya recibidos por el central, por lo que
requiere coordinacion y verificacion de idempotencia. La opcion preferida es
corregir o restaurar una version compatible con chunks.
