# Simplificación aplicada a `crystal_offline`

Fecha: 24 de julio de 2026.

## Resultado

El esquema se redujo al mínimo que mantiene:

- recuperación después de reinicios;
- detección y cierre de eventos;
- reintentos con lease;
- idempotencia;
- envío compatible con `scada_minute` y `scada_event`.

No se eliminó ninguna tabla completa. Las seis tablas tienen una responsabilidad activa:

| Tabla | Por qué se conserva |
|---|---|
| `edge_schema_migration` | Evita aplicar dos veces una migración. |
| `edge_runtime_state` | Recupera la máquina ONLINE/OFFLINE/SYNCING tras un reinicio. |
| `local_outage_session` | Agrupa una desconexión y sus registros. |
| `local_scada_minute` | Buffer destinado a `scada_minute`. |
| `local_scada_event` | Buffer destinado a `scada_event`. |
| `edge_tag_state` | Detecta cambios y relaciona el evento abierto. |

Eliminar cualquiera de ellas quitaría una función solicitada. La simplificación aplicada retira
columnas, no responsabilidades.

## Columnas eliminadas

La migración
[`002_simplify_schema.sql`](../migrations/local/002_simplify_schema.sql) elimina:

| Tabla | Columnas |
|---|---|
| `local_outage_session` | `diagnostic_metadata`, `created_at`, `updated_at` |
| `edge_runtime_state` | `updated_at` |
| `local_scada_minute` | `source_node_id`, `quality`, `created_at` |
| `local_scada_event` | `source_node_id`, `alert_type`, `is_open`, `created_at`, `updated_at` |
| `edge_tag_state` | `last_changed_at`, `last_observed_at`, `updated_at` |

Total: 15 columnas.

También se eliminan:

- `ix_local_scada_minute_tag_range`, duplicado por la restricción única de minuto;
- `ix_edge_tag_state_observed`, porque la columna y la consulta diagnóstica ya no existen.

## Valores derivados

Tres columnas se retiraron sin cambiar el contrato enviado:

| Payload | Derivación |
|---|---|
| `minutes[].quality` | Constante `good`; las lecturas inválidas no se persisten. |
| `events[].alert_type` | Constante `STATE_CHANGE`. |
| `events[].is_open` | `ended_at IS NULL`. |

`source_node_id` continúa en la cabecera autenticada de cada lote y en
`local_outage_session`; no se repite en cada lectura o evento.

## Compatibilidad central

### `scada_minute`

| Local | Central |
|---|---|
| `lagoon_id` | `lagoon_id` |
| `tag_id` | `tag_id` |
| `bucket` | `bucket` |
| `state` | `state` |
| `value_num` | `value_num` |
| `value_bool` | `value_bool` |

La llave idempotente se conserva como `(lagoon_id, tag_id, bucket)`.

### `scada_event`

| Local | Central |
|---|---|
| `id` | `id` |
| `lagoon_id` | `lagoon_id` |
| `tag_id` | `tag_id` |
| `tag_label` | `tag_label` |
| `previous_state` | `previous_state` |
| `state` | `state` |
| `started_at` | `start_ts` |
| `ended_at` | `end_ts` |

La duración se calcula al serializar. El UUID local se conserva para idempotencia.

## Columnas que no deben eliminarse

- `id`, `outage_session_id`: integridad, acuses e idempotencia.
- `captured_at`: requerido por el contrato v1.
- `tag_label`, `previous_state`: forman parte del evento central.
- `sync_status`, intentos, errores, reintento y lease: recuperación de fallos.
- `state`, `value_num`, `value_bool`: preservan tipos reales; no deben convertirse a texto.
- `open_event_id`: permite cerrar correctamente una transición después de reiniciar.

## Aplicación segura

En un servidor existente:

```bash
sudo systemctl stop collector-offline
sudo -u collector_local \
  /opt/crystal/collector-offline/.venv/bin/collector-offline migrate \
  --config /etc/crystal/collector-offline/site.yml
sudo systemctl start collector-offline
```

La migración usa `DROP COLUMN IF EXISTS` y `DROP INDEX IF EXISTS`, por lo que también es segura
para una instalación nueva cuyo `001_initial.sql` ya tiene el esquema reducido.
