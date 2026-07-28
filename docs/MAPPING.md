# Mapeo local a central

El backfill historico usa exclusivamente chunks CSV.gz mediante
/offline-transfer. Nunca llama al endpoint realtime /ingest/scada y no existe
un envio JSON de filas.

## Minutos

| CSV | Origen local | Destino central | Regla |
|---|---|---|---|
| lagoon_id | local_scada_minute.lagoon_id | scada_minute.lagoon_id | Debe pertenecer al nodo autenticado. |
| tag_id | local_scada_minute.tag_id | scada_minute.tag_id | Tag registrado, maximo 64 caracteres. |
| bucket | local_scada_minute.bucket | scada_minute.bucket | UTC y alineado al minuto. |
| state | local_scada_minute.state | scada_minute.state | Uno de state/value_num/value_bool. |
| value_num | local_scada_minute.value_num | scada_minute.value_num | Decimal con punto. |
| value_bool | local_scada_minute.value_bool | scada_minute.value_bool | true/false. |
| source | constante | scada_minute.source | Siempre collector_offline. |

La idempotencia central de minutos es (lagoon_id, tag_id, bucket).

## Eventos

| CSV | Origen local | Destino central | Regla |
|---|---|---|---|
| id | local_scada_event.id | scada_event.id | UUID idempotente preservado. |
| lagoon_id/tag_id | columnas homonimas | columnas homonimas | Identidad funcional. |
| tag_label | local_scada_event.tag_label | scada_event.tag_label | Puede ser vacio. |
| alert_type | constante STATE_CHANGE | scada_event.alert_type | Historico, sin alarmas realtime. |
| previous_state/state | columnas homonimas | columnas homonimas | Estado discreto. |
| start_ts/end_ts | started_at/ended_at | start_ts/end_ts | UTC; evento se cierra al recuperar conectividad. |
| duration_sec | diferencia entera | duration_sec | No negativa. |
| source | constante | scada_event.source | Siempre collector_offline. |

Los campos locales de lease, intentos, paths y estado no se incluyen en el
CSV. Se transmiten en el manifiesto transfer_id, chunk_id, secuencia,
data_kind, conteos, tamanos, SHA-256 y source=collector_offline.

Las filas solo pasan a sent despues de VERIFIED. El importador historico no
ejecuta alarmas, notificaciones, watchdog ni WebSocket.
