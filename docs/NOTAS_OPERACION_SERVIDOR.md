# Notas personales de operación — Collector Offline

Guía rápida para operar y mantener el Collector Offline instalado en Ubuntu.

> El servidor `small_sim` utiliza un único archivo de sitio:
> `/etc/crystal/collector-offline/site.yml`. Para otras instalaciones consulte
> [`REUTILIZACION_NUEVA_LAGUNA.md`](REUTILIZACION_NUEVA_LAGUNA.md).

## 1. Datos de referencia

| Recurso | Valor |
|---|---|
| Servidor | `collector-local` |
| IP del servidor | `192.168.100.20` |
| Usuario Linux administrativo | `adminviga` |
| Servicio systemd | `collector-offline.service` |
| Aplicación | `/opt/crystal/collector-offline` |
| Configuración | `/etc/crystal/collector-offline` |
| Variables protegidas | `/etc/crystal/collector-offline.env` |
| Base de datos local | `crystal_offline` |
| Usuario local/rol PostgreSQL | `collector_local` |
| FastAPI local | `http://127.0.0.1:9187` |
| PLC Rockwell actual | `192.168.100.11` |

Las contraseñas y el token no deben escribirse en este documento ni mostrarse en los logs.

## 2. Conectarse al servidor

Desde PowerShell, CMD o Linux:

```bash
ssh adminviga@192.168.100.20
```

Identificar el servidor y revisar la hora:

```bash
hostname
hostname -I
date
timedatectl
```

Información básica del sistema:

```bash
uptime
df -h
free -h
```

Ver los procesos del Collector:

```bash
pgrep -af collector-offline
```

Comprobar los puertos locales:

```bash
sudo ss -lntp | grep -E '(:5432|:9187)'
```

FastAPI debe escuchar únicamente en `127.0.0.1:9187`. PostgreSQL no debe publicarse hacia la
red de planta.

## 3. Operación del servicio

### Ver el estado

```bash
sudo systemctl status collector-offline --no-pager -l
```

Respuesta corta (`active` o `inactive`):

```bash
systemctl is-active collector-offline
```

Comprobar si inicia automáticamente con Ubuntu:

```bash
systemctl is-enabled collector-offline
```

Ver PID, cantidad de reinicios y hora de inicio:

```bash
sudo systemctl show collector-offline \
  -p ActiveState \
  -p SubState \
  -p MainPID \
  -p NRestarts \
  -p ActiveEnterTimestamp
```

### Iniciar o ejecutar

```bash
sudo systemctl start collector-offline
```

Iniciar ahora y habilitar el arranque automático:

```bash
sudo systemctl enable --now collector-offline
```

### Reiniciar

Use este comando después de cambiar un YAML o actualizar la aplicación:

```bash
sudo systemctl restart collector-offline
```

Luego confirme:

```bash
sudo systemctl status collector-offline --no-pager -l
sudo journalctl -u collector-offline -n 50 --no-pager -o cat
```

### Detener

```bash
sudo systemctl stop collector-offline
```

Confirmar que se detuvo:

```bash
systemctl is-active collector-offline
```

Debe responder `inactive`.

### Evitar que inicie con Ubuntu

```bash
sudo systemctl disable collector-offline
```

Para volver a habilitarlo:

```bash
sudo systemctl enable collector-offline
```

### Si se modifica el archivo `.service`

```bash
sudo systemctl daemon-reload
sudo systemctl restart collector-offline
```

No ejecute manualmente una segunda instancia mientras systemd tenga una activa. La aplicación
usa un bloqueo en PostgreSQL y rechazará la instancia duplicada.

## 4. Logs

Seguir los logs en vivo:

```bash
sudo journalctl -u collector-offline -f -o cat
```

Ver las últimas 100 líneas:

```bash
sudo journalctl -u collector-offline -n 100 --no-pager -o cat
```

Logs desde hoy:

```bash
sudo journalctl -u collector-offline --since today --no-pager -o cat
```

Logs de la última hora:

```bash
sudo journalctl -u collector-offline --since "1 hour ago" --no-pager -o cat
```

Buscar advertencias y errores:

```bash
sudo journalctl -u collector-offline --since today --no-pager -o cat |
  grep -Ei 'warning|error|failed|exception'
```

Mensajes importantes:

- `Node state transition`: cambio entre `online`, `offline` y `syncing`.
- `Connected to Rockwell PLC`: conexión correcta al PLC.
- `Offline capture completed`: captura local realizada.
- `Chunk sealed`: CSV.gz atomico listo para transferencia.
- `Chunk part confirmed`: parte recibida o ya existente en el central.
- `Central chunk status applied`: progreso de importacion o VERIFIED.
- `Tag read unavailable`: un tag no pudo leerse.
- `PLC read failed`: falló la lectura del PLC.

Revisar cuánto disco ocupa journald:

```bash
sudo journalctl --disk-usage
```

Opcional, conservar solo los últimos 14 días de logs:

```bash
sudo journalctl --vacuum-time=14d
```

Este último comando elimina logs más antiguos; úselo solamente cuando ya no sean necesarios.

## 5. Estado operativo de FastAPI

Comprobar que el proceso responde:

```bash
curl --fail --silent http://127.0.0.1:9187/health/live
```

Comprobar readiness:

```bash
curl --fail --silent http://127.0.0.1:9187/health/ready
```

Ver el estado completo:

```bash
curl --silent --show-error http://127.0.0.1:9187/status |
  python3 -m json.tool
```

Actualizar el estado en pantalla cada cinco segundos:

```bash
watch -n 5 'curl -s http://127.0.0.1:9187/status | python3 -m json.tool'
```

Interpretación:

- `online`: backend central disponible; no se realiza captura local.
- `offline`: backend central no disponible; se lee el PLC y se guarda localmente.
- `syncing`: volvió la conexión y se está enviando el backlog.
- `last_health_ok: true`: último health check exitoso.
- `pending`: filas esperando envío.
- `syncing`: filas arrendadas por el proceso de sincronización.
- `sent`: filas aceptadas por el backend central.
- `failed`: filas rechazadas y pendientes de revisión manual.

En operación normal, el estado esperado es `online` y `pending`, `syncing` y `failed` deben estar
en cero.

## 6. Configuración

Ver los archivos disponibles:

```bash
sudo ls -la /etc/crystal/collector-offline
```

Editar la configuración completa del sitio:

```bash
sudoedit /etc/crystal/collector-offline/site.yml
```

Configuración Rockwell actual:

```yaml
source: rockwell

rockwell:
  ip: "192.168.100.11"
  slot: 0
  timeout_sec: 5

tags:
  Motor: "Motor"
  Partir: "Partir"
  CONTADOR: "CONTADOR"

tag_options:
  CONTADOR:
    data_type: int
    event: false

offline:
  central:
    health_attempts: 1
  supervisor:
    health_interval_sec: 30
    failure_threshold: 3
    recovery_threshold: 2
```

La clave izquierda (`Motor`, `Partir`, `CONTADOR`) es el identificador guardado y enviado al
backend. El valor derecho es el nombre exacto, sensible a mayúsculas, leído desde el PLC.

Después de modificar un YAML:

```bash
sudo systemctl restart collector-offline
sudo systemctl status collector-offline --no-pager -l
sudo journalctl -u collector-offline -n 100 --no-pager -o cat
```

No muestre ni copie el contenido de `/etc/crystal/collector-offline.env`: contiene el token y la
conexión de la base de datos.

## 7. Comandos de PostgreSQL

El propósito y la definición de todas las tablas y columnas están documentados en
[DICCIONARIO_BASE_DATOS.md](DICCIONARIO_BASE_DATOS.md).

La propuesta para reducir columnas e índices, junto con la validación contra `scada_minute` y
`scada_event`, está en
[ANALISIS_SIMPLIFICACION_BD.md](ANALISIS_SIMPLIFICACION_BD.md).

### Entrar a la base local

Como usuario operativo:

```bash
sudo -u collector_local psql -d crystal_offline
```

Como administrador PostgreSQL:

```bash
sudo -u postgres psql -d crystal_offline
```

Salir de `psql`:

```text
\q
```

Comandos útiles dentro de `psql`:

```text
\conninfo
\dt
\d local_scada_minute
\d local_scada_event
\d local_outage_session
```

### Estado interno del Collector

```bash
sudo -u collector_local psql -d crystal_offline -x -c \
"SELECT * FROM edge_runtime_state WHERE id = 1;"
```

### Últimas desconexiones

```bash
sudo -u collector_local psql -d crystal_offline -c \
"SELECT id,
        status,
        started_at,
        connectivity_restored_at,
        sync_completed_at,
        failure_reason
 FROM local_outage_session
 ORDER BY started_at DESC
 LIMIT 10;"
```

### Cantidad de lecturas por estado de sincronización

```bash
sudo -u collector_local psql -d crystal_offline -c \
"SELECT sync_status, COUNT(*)
 FROM local_scada_minute
 GROUP BY sync_status
 ORDER BY sync_status;"
```

### Últimas lecturas, conservando su tipo

```bash
sudo -u collector_local psql -d crystal_offline -c \
"SELECT tag_id,
        bucket,
        state,
        value_num,
        value_bool,
        sync_status
 FROM local_scada_minute
 ORDER BY bucket DESC, tag_id
 LIMIT 30;"
```

Cada fila utiliza solo una columna de valor:

- estados discretos: `state`;
- mediciones numéricas: `value_num`;
- booleanos: `value_bool`.

Por eso es normal que las otras dos columnas aparezcan como `NULL`.

### Revisar específicamente Motor, Partir y CONTADOR

```bash
sudo -u collector_local psql -d crystal_offline -c \
"SELECT tag_id,
        bucket,
        value_num,
        value_bool,
        sync_status,
        captured_at
 FROM local_scada_minute
 WHERE tag_id IN ('Motor', 'Partir', 'CONTADOR')
 ORDER BY bucket DESC, tag_id
 LIMIT 45;"
```

PostgreSQL puede mostrar `f` para `false` y `t` para `true`. `Motor` y `Partir` usan
`value_bool`; `CONTADOR` usa `value_num`.

### Revisar eventos

```bash
sudo -u collector_local psql -d crystal_offline -c \
"SELECT tag_id,
        previous_state,
        state,
        started_at,
        ended_at,
        (ended_at IS NULL) AS is_open,
        sync_status
 FROM local_scada_event
 ORDER BY started_at DESC
 LIMIT 30;"
```

Que no existan eventos es normal si `Motor` y `Partir` no cambiaron durante la desconexión.

### Revisar filas con errores

```bash
sudo -u collector_local psql -d crystal_offline -c \
"SELECT tag_id,
        bucket,
        sync_attempts,
        last_sync_error,
        next_retry_at
 FROM local_scada_minute
 WHERE sync_status = 'failed'
 ORDER BY bucket;"
```

No elimine manualmente registros `pending`, `syncing` o `failed`.

### Tamaño de la base y de sus tablas

```bash
sudo -u postgres psql -d crystal_offline -c \
"SELECT pg_size_pretty(pg_database_size('crystal_offline')) AS database_size;"
```

```bash
sudo -u postgres psql -d crystal_offline -c \
"SELECT relname AS table_name,
        pg_size_pretty(pg_total_relation_size(relid)) AS total_size
 FROM pg_catalog.pg_statio_user_tables
 ORDER BY pg_total_relation_size(relid) DESC;"
```

## 8. Mantenimiento

### Revisión diaria

```bash
systemctl is-active collector-offline
curl -s http://127.0.0.1:9187/status | python3 -m json.tool
sudo journalctl -u collector-offline --since today --no-pager -o cat |
  grep -Ei 'error|failed|exception'
df -h
```

Comprobar que no exista backlog fallido:

```bash
sudo -u collector_local psql -d crystal_offline -c \
"SELECT sync_status, COUNT(*)
 FROM local_scada_minute
 GROUP BY sync_status;"
```

### Respaldo de PostgreSQL

Crear una vez el directorio protegido:

```bash
sudo install -d -m 0750 -o postgres -g postgres \
  /var/backups/crystal-offline
```

Crear un respaldo:

```bash
sudo -u postgres pg_dump -Fc crystal_offline \
  -f "/var/backups/crystal-offline/crystal_offline-$(date +%F-%H%M).dump"
```

Listar respaldos:

```bash
sudo ls -lh /var/backups/crystal-offline
```

Comprobar que el último archivo sea legible:

```bash
sudo -u postgres pg_restore --list \
  "/var/backups/crystal-offline/NOMBRE_DEL_ARCHIVO.dump" |
  head
```

### Respaldo de configuración

```bash
sudo install -d -m 0700 /var/backups/collector-offline-config
sudo cp -a /etc/crystal/collector-offline \
  "/var/backups/collector-offline-config/config-$(date +%F-%H%M)"
sudo cp -a /etc/crystal/collector-offline.env \
  "/var/backups/collector-offline-config/env-$(date +%F-%H%M)"
sudo cp -a /opt/crystal/collector-offline \
  "/var/backups/collector-offline-config/app-$(date +%F-%H%M)"
sudo cp -a /etc/systemd/system/collector-offline.service \
  "/var/backups/collector-offline-config/unit-$(date +%F-%H%M)"
sudo tar --acls --xattrs -C /var/lib -czf \
  "/var/backups/collector-offline-config/outbox-$(date +%F-%H%M).tar.gz" \
  crystal-offline
```

Ese respaldo contiene secretos y debe permanecer accesible únicamente para `root`.

### Mantenimiento de PostgreSQL

Actualizar estadísticas del optimizador:

```bash
sudo -u postgres vacuumdb --analyze crystal_offline
```

PostgreSQL ejecuta autovacuum automáticamente. No use `VACUUM FULL` durante operación normal:
bloquea tablas y requiere espacio adicional.

### Actualización de la aplicación

Antes de actualizar:

```bash
sudo systemctl stop collector-offline
```

Realice el respaldo de PostgreSQL y configuración, instale la nueva versión y luego:

```bash
sudo systemctl daemon-reload
sudo systemctl start collector-offline
sudo systemctl status collector-offline --no-pager -l
sudo journalctl -u collector-offline -n 100 --no-pager -o cat
```

## 9. Diagnóstico rápido

### El servicio no inicia

```bash
sudo systemctl status collector-offline --no-pager -l
sudo journalctl -u collector-offline -n 200 --no-pager -o cat
sudo systemctl cat collector-offline
```

### FastAPI no responde

```bash
systemctl is-active collector-offline
pgrep -af collector-offline
sudo ss -lntp | grep 9187
sudo journalctl -u collector-offline -n 100 --no-pager -o cat
```

### No se guardan lecturas

El Collector solo captura localmente cuando el estado es `offline`.

```bash
curl -s http://127.0.0.1:9187/status | python3 -m json.tool
sudo journalctl -u collector-offline -f -o cat
```

Busque `Connected to Rockwell PLC`, `Offline capture completed`, `Tag read unavailable` o
`PLC read failed`.

### Existen filas `failed`

Primero corrija la causa indicada en `last_sync_error`. Después reencole mediante el comando
oficial:

```bash
sudo systemctl stop collector-offline
sudo bash -c '
  set -a
  source /etc/crystal/collector-offline.env
  set +a
  runuser -u collector_local -- \
    /opt/crystal/collector-offline/.venv/bin/collector-offline retry-failed \
    --config /etc/crystal/collector-offline/site.yml
'
sudo systemctl start collector-offline
```

No reencole continuamente sin corregir antes el token, la autorización o el tag rechazado.

### Verificación final después de una intervención

```bash
systemctl is-active collector-offline
curl -s http://127.0.0.1:9187/status | python3 -m json.tool
sudo journalctl -u collector-offline -n 50 --no-pager -o cat
sudo -u collector_local psql -d crystal_offline -c \
"SELECT sync_status, COUNT(*)
 FROM local_scada_minute
 GROUP BY sync_status;"
```

Resultado esperado:

- servicio `active`;
- API en `online` cuando el servidor central está disponible;
- ningún registro `pending`, `syncing` o `failed`;
- registros confirmados con estado `sent`.
