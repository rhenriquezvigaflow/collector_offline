# Diccionario de crystal_offline

Esquema vigente despues de 004_chunk_transfer.sql.

## Relaciones

~~~mermaid
erDiagram
    local_outage_session ||--o{ local_scada_minute : contiene
    local_outage_session ||--o{ local_scada_event : contiene
    local_outage_session o|--o| edge_runtime_state : sesion_actual
    local_scada_event o|--o| edge_tag_state : evento_abierto
    transfer_chunk ||--o{ local_scada_minute : agrupa
    transfer_chunk ||--o{ local_scada_event : agrupa
~~~

## edge_schema_migration

version es el nombre unico del SQL aplicado; applied_at registra su fecha.

## local_outage_session

Agrupa una desconexion:

- id, source_node_id, lagoon_id y started_at;
- connectivity_restored_at y sync_completed_at;
- status offline, syncing o completed;
- failure_reason.

Solo existe una sesion activa por nodo/laguna.

## edge_runtime_state

Fila singleton id=1:

- state, failure_count, recovery_count y current_session_id;
- last_health_at, last_health_ok, last_sync_at y last_error;
- next_chunk_sequence: contador durable que no se reinicia cuando se elimina
  metadata antigua.

## local_scada_minute

Una lectura por lagoon_id, tag_id y bucket UTC:

- id y outage_session_id;
- source fijo collector_offline;
- state, value_num o value_bool: exactamente uno no NULL;
- captured_at;
- sync_status: pending, syncing, chunked, sent o failed;
- sync_attempts, last_sync_error, next_retry_at y synced_at;
- lease_until/lease_token;
- chunk_id, NULL antes del claim.

La llave (lagoon_id, tag_id, bucket) es unica.

## local_scada_event

Segmento de cambio discreto:

- id UUID preservado y outage_session_id;
- source fijo collector_offline;
- lagoon_id, tag_id, tag_label;
- previous_state, state, started_at y ended_at;
- las mismas columnas sync/lease/chunk_id del minuto.

Solo puede existir un evento ended_at IS NULL por laguna/tag. Al recuperar
conectividad se cierra antes de crear el chunk.

## transfer_chunk

| Grupo | Columnas |
|---|---|
| Identidad | chunk_id, transfer_id, sequence_number, source, source_node_id, lagoon_id, data_kind |
| Archivo | file_path, manifest_path, from/to timestamp, row_count, tamanos, sha256 |
| Partes | part_size_bytes, total_parts, confirmed_parts |
| Estado | status, retry_count, next_retry_at, last_error, lease_until, lease_token |
| Importacion | staged_rows, inserted_rows, duplicate_rows |
| Auditoria | created_at, sealed_at, uploaded_at, verified_at, file_deleted_at |

source tiene DEFAULT/CHECK collector_offline. Los estados permitidos son
CREATING, SEALED, PENDING, UPLOADING, UPLOADED, IMPORTING, VERIFIED, FAILED y
QUARANTINED.

FAILED recibido desde el import central no se persiste como terminal local:
permanece UPLOADED/IMPORTING y vuelve a ejecutar complete con backoff. FAILED local representa
un error permanente del archivo/transporte o una creacion abandonada.

## edge_tag_state

lagoon_id/tag_id forman la clave, last_value conserva el estado discreto y
open_event_id apunta al evento abierto. Se limpia al iniciar una desconexion.

## Consultas

~~~sql
SELECT status, count(*)
FROM transfer_chunk
GROUP BY status
ORDER BY status;

SELECT sync_status, count(*)
FROM (
    SELECT sync_status FROM local_scada_minute
    UNION ALL
    SELECT sync_status FROM local_scada_event
) backlog
GROUP BY sync_status
ORDER BY sync_status;

SELECT chunk_id, transfer_id, sequence_number, data_kind, status,
       row_count, confirmed_parts, total_parts, last_error
FROM transfer_chunk
ORDER BY sequence_number DESC
LIMIT 30;
~~~
