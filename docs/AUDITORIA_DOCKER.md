# Auditoría previa a la dockerización

Inspección realizada el 19 de noviembre de 2026 en `C:\Proyectos\collector-offline`.
Referencia funcional: documento «Collector Offline», fechado el 17 de junio de 2026.
Las instrucciones de trabajo proceden de la solicitud del usuario; el documento se trata
como referencia, con prioridad al comportamiento implementado.

## Diagnóstico antes de modificar

La implementación es compatible con el modelo de contingencia descrito. No requiere
reescribir los drivers, separar workers ni crear una cola de archivos. La aplicación es
un proceso Python único con una API local en otro hilo. Docker debe mantener esta unidad
y separar únicamente PostgreSQL.

Se revisaron los archivos fuente, YAML, SQL, pruebas, documentación, manifiestos,
unidad systemd y scripts. Los entornos virtuales son artefactos, no código del proyecto.
Los archivos SSH `rhenriquez` y `rhenriquez.pub` estaban versionados: se identificaron por
nombre/tipo, sin imprimir su contenido. No son necesarios para ejecutar el Collector.
No se encontró dump de PostgreSQL; la referencia de esquema disponible es
`migrations/local/001_initial.sql`. No se deduce la versión del motor a partir del SQL.

## Aplicación y procesos

`collector-offline` entra por `cli.main`; `python -m collector_offline` usa el mismo CLI.
Los comandos existentes son `run`, `migrate`, `status` y `retry-failed`.
`EdgeRuntime` ejecuta secuencialmente health, captura, sync y limpieza.
`StateCoordinator` concentra las transiciones; no existen subprocessos ni servicios
separados para Supervisor, Collector y Sync Worker. `StatusServer` usa un hilo Uvicorn.
SIGTERM/SIGINT activan un evento y cierran lectores, HTTP y servidor local.
Los logs JSON salen por stdout/stderr; no existe directorio de spool ni logs de aplicación.

Python requiere >=3.12. Las dependencias originales se declaran en `pyproject.toml` y
los requirements originales instalan el paquete editable con extras. No había lockfile.
Los extras PLC contienen `opcua` y `pycomm3`. Las versiones exactas resultantes de esta
implementación se guardan en `requirements.lock` y `requirements-build.lock`.

## PLC y configuración

Drivers reales: Siemens OPC UA con cliente síncrono `opcua`, Rockwell EtherNet/IP con
`pycomm3.LogixDriver`, y simulador para pruebas. No se implementan Modbus ni protocolos nuevos.
Rockwell lee tags en batch con conexión persistente; timeout por defecto 5 s,
rotación 3600 s y cierre tras 10 excepciones consecutivas. Siemens agrupa nodos,
reconecta tras errores y permite varios módulos; las credenciales se referencian por
nombres de variables. No se escribe al PLC.

La captura ocurre solo OFFLINE y una vez por bucket UTC, cada 60 s o un múltiplo de 60.
`poll_seconds` del YAML central se ignora: no controla la captura local.
Se conserva el codec de bool, numéricos y estados, y el reconocimiento de tags de eventos.
Las lecturas inválidas se omiten con logs; no se inventan valores de sustitución.

La configuración actual selecciona Rockwell `192.168.100.11` (puerto de protocolo
44818/TCP), tags `Motor`/`Partir`, nodo `edge-small-sim-01`, laguna `small_sim` y backend
privado `192.168.2.100:8090`. Se admite HTTP privado únicamente por opt-in explícito.
Estos datos no se copian a la imagen ni al paquete genérico.

## PostgreSQL

El SQL crea las cuatro tablas del documento, más `edge_runtime_state`; el migrador añade
`edge_schema_migration`. No requiere TimescaleDB, UUID extensions ni otras extensiones:
los UUID los genera Python. El motor usa transacciones SQLAlchemy, UTC, pool con pre-ping,
statement timeout 10 s y lock timeout 2 s por defecto. La versión de motor actualmente
instalada en la planta no puede verificarse desde este entorno.

Existen FKs restrictivas hacia sesiones, unicidad `(lagoon_id, tag_id, bucket)`, UUID de
eventos, índice único parcial de eventos abiertos y de sesión activa, índices de pendientes,
sesiones y rangos, y constraints de valor único/estado/lease/fechas en el SQL.
El ORM conserva las mismas columnas y tablas, pero no replica todos los constraints e
índices PostgreSQL: SQLite de pruebas no reemplaza la validación del SQL real.
Se utiliza el migrador SQL existente en Docker, nunca `metadata.create_all` en operación.

Todo el backlog, estados, leases, sesiones y estado discreto reside en PostgreSQL.
No hay columna ni literal `collector_offline` de origen en el payload/SQL local inspeccionado;
la documentación atribuye ese origen al servicio central. No se añade ni cambia un campo
del contrato. La comprobación del origen escrito por el servidor queda pendiente allí.

## Sincronización y fallos

HTTP POST `/ingest/offline/v1/backfill`, JSON v1, Bearer por nodo; no chunks de archivos.
Lotes de 250 por defecto; claims con `FOR UPDATE SKIP LOCKED`, token y lease de 120 s.
El advisory lock de conexión impide otra instancia sobre la misma BD.
Tras crash, leases vencidos regresan a pending al inicializar y en cada claim.
Los no vencidos esperan hasta expirar, sin asumir pérdida ni marcarlos sent.
Solo `accepted`/`duplicate` explícitos por `kind/local_id` confirman registros.
HTTP 429/5xx/red reintenta con backoff y jitter; errores permanentes/agotamiento quedan
failed y requieren `retry-failed`. Se preservan UUID y claves centrales.

ONLINE no lee PLC. Tras 3 fallos se abre sesión OFFLINE. Dos health exitosos permiten
SYNCING y cierran primero eventos/captura; una nueva caída durante sync vuelve a OFFLINE
en la misma sesión. La sesión solo finaliza cuando todos sus registros están sent.
Los readiness requieren `status=ready` y `checks.database=ok`; ping/HTTP 200 solo no bastan.
El cliente no llama al endpoint realtime. La ausencia de efectos WS/notificaciones central
depende del contrato existente, que esta tarea no modifica ni verifica en producción.

## Diferencias y riesgos

| Tema | Documento | Código preservado |
|---|---|---|
| Orquestación | Cuatro componentes conceptuales | Un proceso de aplicación y PostgreSQL separado |
| SO | Ubuntu Server 24.04 | Docker sobre Ubuntu Desktop 24.04, sin sesión gráfica necesaria |
| Microcortes | 2 o 3 fallos | 3 fallos, 2 éxitos para recuperar |
| Retención | Recomendación 30 días | 90 días desde sync, por lotes; no se reduce automáticamente |
| Pendientes | Estados por registro | Estados más leases y tokens persistentes |
| Transferencia | Lotes | JSON HTTP, sin spool/chunks en filesystem |
| Tiempo | Zona IANA | Instantes y buckets UTC; IANA como configuración validada |

La limpieza existente conserva pending/failed/syncing, eventos abiertos y sesiones sin
completar. No se ejecuta sobre datos de planta en esta tarea.
No hay Docker Engine local ni servidor central/PLC de prueba autorizado disponible.
Las claves SSH ya versionadas requieren revisión del administrador antes de publicar el
repositorio privado; excluirlas del build no las elimina del historial Git.
