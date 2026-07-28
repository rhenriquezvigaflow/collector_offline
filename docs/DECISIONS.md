# Decisiones, supuestos y riesgos

## Decisiones

- La base local crystal_offline es independiente de crystal_plc.
- El loop principal conserva health, captura y estado; gzip/fsync/HTTP viven en
  un thread separado.
- PostgreSQL usa advisory lock de instancia, locks por filas, leases durables y
  una relacion fila/chunk.
- source es siempre collector_offline. Nodo y laguna son campos separados.
- La llave minuto central es (lagoon_id, tag_id, bucket); eventos conservan UUID.
- Los eventos abiertos se cierran en connectivity_restored_at antes de crear su
  chunk.
- El transporte es CSV.gz por partes, con SHA-256 e idempotencia. Solo VERIFIED
  permite marcar sent o aplicar retencion.
- El contador next_chunk_sequence vive fuera de metadata de chunks para no
  reiniciarse cuando se limpia auditoria antigua.

## Supuestos de despliegue

- Produccion publica /api/offline-transfer detras de HTTPS IIS con TLS valido.
- Los tags ya fueron registrados por el flujo realtime central.
- El DBA aplica scripts/create_offline_transfer.sql y aprovisiona un token por
  nodo.
- El limite central permite al menos 20.000 filas, 25 MiB sin comprimir y partes
  de 1 MiB; edge impide superar 20.000, 25 MiB y 2 MiB por parte.

## Riesgos residuales

- La copia local del YAML puede quedar desactualizada. Cada cambio de tags debe
  copiarse y validarse en planta.
- FAILED del import central puede repetirse por indisponibilidad; edge conserva
  archivo y continua polling con backoff hasta VERIFIED o QUARANTINED.
- Un downgrade a codigo sin chunks exige coordinacion con el import central y
  requeue transaccional de datos no VERIFIED; restaurar una base antigua puede
  perder backlog reciente.
- Disco y backups deben incluir PostgreSQL y /var/lib/crystal-offline/outbox.
