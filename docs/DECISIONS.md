# Decisiones, supuestos y riesgos

## Decisiones

- La BD local es independiente: `crystal_offline`. No comparte tablas ni credenciales con
  `crystal_plc`.
- Un solo proceso coordina health, captura, sync y limpieza. PostgreSQL impide una segunda
  instancia mediante advisory lock, y cada claim además usa `FOR UPDATE SKIP LOCKED` + lease.
- Se mantiene una copia local de `small_sim.yml`. No existe dependencia de archivos del
  Collector Central durante una caída.
- La llave central de minuto es `(lagoon_id, tag_id, bucket)`; agregar `source_node_id`
  alteraría el significado existente y permitiría duplicados.
- Los eventos abiertos se cierran en `connectivity_restored_at`. Desde ese instante el
  Collector Central vuelve a ser dueño del flujo realtime.
- Calidad no existe en `scada_minute`. Solo se persisten/sincronizan observaciones compatibles;
  fallas o valores no tipables se registran por tag sin detener el ciclo.
- La API local de operación y la API central de backfill usan FastAPI.

## Supuestos pendientes de despliegue

- El backend está detrás de TLS válido en producción; HTTP solo se acepta para loopback.
- Los tags de la copia local ya fueron registrados por el flujo realtime en
  `collector_tag_registry`.
- El DBA aplica `create_offline_backfill.sql` y aprovisiona un token distinto por nodo.

## Riesgos residuales

- Una copia local de YAML puede quedar desactualizada. Toda modificación de tags debe copiarse
  y validarse durante el cambio de configuración de la planta.
- La migración y el advisory lock sí se probaron contra PostgreSQL 18 en una instancia efímera.
  La base permanente `crystal_offline` no pudo crearse en la instancia instalada porque el
  único usuario disponible (`userweb`) no posee `CREATEDB`; debe ejecutar el script un DBA/root.
- `small_sim.yml` contiene un endpoint OPC UA de red. Un firewall de planta debe permitir solo
  ese destino/puerto y el HTTPS saliente al backend central.
