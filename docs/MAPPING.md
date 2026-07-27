# Mapeo local → central

El esquema se deriva de los modelos reales de `crystal-backend`. El backfill no llama a
`/ingest/scada`: escribe únicamente histórico por el endpoint FastAPI
`/ingest/offline/v1/backfill`.

| Local | Payload v1 | Central | Regla |
|---|---|---|---|
| `source_node_id` | `source_node_id` | `offline_collector_node.source_node_id` | Autenticación/autorización; no cambia la llave SCADA. |
| `lagoon_id` | `lagoon_id` | `scada_minute.lagoon_id`, `scada_event.lagoon_id` | Debe coincidir con el nodo autorizado y una laguna habilitada. |
| `local_scada_minute.id` | `minutes[].local_id` | no persistido | Identifica el acuse local; la idempotencia central usa la llave real. |
| `tag_id` | `minutes[].tag_id` | `scada_minute.tag_id` | Debe existir en `collector_tag_registry`. Máximo 64 caracteres. |
| `bucket` | `minutes[].bucket` | `scada_minute.bucket` | UTC, alineado al minuto. |
| `state/value_num/value_bool` | igual | igual | Exactamente uno no nulo, según `value_codec` central. |
| `quality` | `quality=good` | no existe | Calidad mala no se envía; la lectura inválida queda en logs locales. |
| `(lagoon_id, tag_id, bucket)` | — | `uq_scada_minute` | Llave idempotente central, sin `source_node_id`. |
| `local_scada_event.id` | `events[].local_id` | `scada_event.id` | UUID local preservado como llave idempotente. |
| `started_at/ended_at` | igual | `start_ts/end_ts` | UTC; un reenvío solo puede cerrar un evento del mismo UUID. |
| `is_open` | `events[].is_open` | derivado de `end_ts IS NULL` | Al recuperar conectividad, el segmento offline abierto se cierra en el límite de recuperación. |
| `duration_sec` | igual | `scada_event.duration_sec` | Diferencia entera no negativa. |

Los campos locales de lease, intentos, error y estado de sync nunca se envían. Un registro
solo pasa a `sent` tras recibir `accepted` o `duplicate` para su `kind + local_id`.

## Efectos excluidos

El servicio `offline_backfill_service` no importa ni ejecuta el estado realtime, evaluación de
alarmas, notificaciones, watchdog ni WebSocket. Los datos conservan sus timestamps históricos.
