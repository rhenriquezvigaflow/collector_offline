# Operación y mantenimiento

Ejecute desde la distribución instalada, normalmente `/opt/crystal/collector-offline`,
con los mismos .env, proyecto Compose y runtime. Nunca ejecute `down --volumes` ni elimine
volúmenes en operación. Las herramientas nuevas están en la raíz; `scripts/install.sh`
y la unidad systemd originales son únicamente el método legado sin Docker.

## Estado y parada

```bash
sudo bash start.sh
sudo bash status.sh
sudo bash diagnose.sh
sudo docker compose logs -f --tail=100 collector
sudo docker compose logs --tail=100 postgres
sudo bash stop.sh
```

`stop.sh` da SIGTERM y conserva volúmenes. `start.sh` valida y usa imágenes locales sin pull
ni build. Estado OFFLINE con BD saludable es una contingencia válida. Los healthchecks no
demuestran lecturas PLC: confirme `minute_rows/event_rows`, tiempos, calidad de logs y
backlog. No exponga la API local a la LAN; se consulta por `docker compose exec`.
Si hay failed, corrija el motivo antes de reencolar explícitamente:

```bash
sudo docker compose exec collector python -m collector_offline.container retry-failed
```

La limpieza original está habilitada a 90 días desde la confirmación, con exclusiones de
pendientes/failed/syncing/eventos abiertos/sesiones incompletas. No hay limpieza de spool.
Supervise `df -h` y `docker system df`; nunca use prune de volúmenes para liberar espacio.
Los logs se rotan automáticamente (50 MB por servicio aproximadamente, más metadatos).

## Respaldo y recuperación

```bash
sudo bash backup.sh
# Ejemplo de destino explícito en un directorio ya creado y protegido:
sudo bash backup.sh /mnt/respaldo/offline-20261119.dump
```

`pg_dump --format=custom --no-owner --no-acl` crea snapshot consistente incluso con captura
en marcha. Se valida el índice con `pg_restore --list`, se publica el archivo atómicamente
y se genera .sha256 portable. Esto comprueba formato/integridad, **no prueba una restauración**.
Para un punto de corte previo a actualizar, update.sh detiene primero el Collector.
Copie dump/hash a almacenamiento externo protegido. Los respaldos contienen datos de planta.
Respalde también .env, runtime/config y secretos por un canal cifrado autorizado: el dump
no contiene passwords de roles y el paquete USB genérico no incluye configuración.
La periodicidad la define el operador mediante cron/timer y una retención de respaldos
corporativa; los scripts no eliminan respaldos antiguos automáticamente.

Restaure únicamente en una instalación/volumen **nuevo**, con configuración del mismo nodo,
versiones de PostgreSQL compatibles e init de roles terminado, antes de arrancar el Collector:

```bash
sudo docker compose up -d --wait postgres
sudo bash restore.sh /mnt/respaldo/offline-20261119.dump --into-empty-database
sudo bash start.sh
sudo bash status.sh
```

restore.sh verifica SHA256, detiene Collector, rechaza BD con tablas y ejecuta
pg_restore en una sola transacción con rol propietario de aplicación. No usa --clean,
TRUNCATE, DROP ni reemplaza históricos. Si la instalación nueva ya arrancó el Collector,
su BD ya tiene tablas y la restauración se rechazará: utilice otra instalación vacía,
no borre las tablas para forzarla. Mantenga el origen apagado para evitar dos lectores.
Restaurar un respaldo antiguo no recupera pendientes capturados después del respaldo;
conserve siempre el volumen actual y reconcilie esos datos antes de cualquier cambio.

## Actualización y rollback

```bash
sudo bash update.sh ghcr.io/organizacion/collector-offline:0.1.1
```

Descarga una referencia versionada, detiene captura, crea dump y copia .env previo,
cambia solo COLLECTOR_IMAGE y verifica salud. Si falla, deja Collector detenido y
mantiene PostgreSQL/volumen: no hace rollback de esquema automáticamente.
Las migraciones nuevas deben ser compatibles con la versión anterior si se necesita
rollback inmediato. El SQL de esta entrega no añade ni modifica el esquema existente.

Para volver a una imagen anterior **solo si el esquema sigue siendo compatible**:

```bash
sudo bash update.sh ghcr.io/organizacion/collector-offline:0.1.0 --offline
```

La imagen anterior debe estar importada/local. Para recuperar también .env anterior,
compare `backups/env-FECHA` y restaure su contenido de forma administrativa, manteniendo el
mismo proyecto/BD. No restaure un dump antiguo sobre el volumen actual para hacer rollback:
podría perder pendientes recientes. Cuando haya cambios incompatibles, restaure en otra
instalación vacía y reconcilie datos; deje el volumen original intacto.
No actualice PostgreSQL mayor cambiando solo el tag. Planifique dump/restore separado.
Cambios en Compose/scripts se distribuyen como una versión completa revisada; update.sh
actualiza la imagen de aplicación, no descarga ni sobrescribe código operativo del host.

## Configuración y secretos

Detenga Collector antes de sustituir el YAML. Conserve la identidad de nodo/laguna si
mantiene el mismo volumen; una planta distinta necesita volumen/proyecto propio.

```bash
sudo docker compose stop collector
sudo bash configure.sh --site /ruta/config-aprobada/site.yml --replace-config
sudo bash start.sh
```

No regenere passwords por reiniciar: configure.sh conserva los existentes. Para rotar el
token, coordine su autorización central previamente, detenga Collector y sustituya el
archivo `runtime/secrets/central_token` de forma administrativa con UID/GID 10001 y modo
0400; recree Collector. Cambiar un password PostgreSQL exige ALTER ROLE autenticado y
actualizar el archivo, no solo editar .env ni esperar que el init se repita.

## Distribución sin Internet

En estación de preparación con imágenes descargadas y .env que selecciona versiones:

```bash
sudo bash export-usb.sh /media/usb/collector-0.1.0
```

Incluye docker save de las dos imágenes, distribución de Compose/scripts/documentos,
lista de imágenes y hashes. Crea aliases `collector-offline-usb/...:sha-ID` y `.env.usb`:
docker save/load no garantiza conservar las referencias de digest de un registry. Se
verifican los IDs locales al importar y se usan los aliases offline sin resolver GHCR.
No incluye .git, claves SSH, YAML productivos, secretos,
backups ni volúmenes. SHA256 comprueba corrupción, no autenticidad: use un USB entregado
por un administrador o una firma corporativa del manifiesto por canal independiente.

En el mini PC con Docker/Compose/python3 ya aprovisionados:

```bash
# import-usb.sh debe proceder de la distribución aprobada, no de un remitente desconocido.
sudo bash import-usb.sh /media/usb/collector-0.1.0 /opt/crystal/collector-offline
cd /opt/crystal/collector-offline
sudo cp .env.usb .env
sudo chmod 600 .env
sudoedit .env  # parámetros del mini PC; preserve los aliases importados
sudo bash install.sh --offline --site /ruta/site.yml --token-file /ruta/token
```

Para actualizar offline, verifique SHA256SUMS del nuevo paquete y ejecute
`sudo docker load --input /media/usb/collector-0.1.1/images.tar`, después
`sudo bash update.sh ALIAS_COLLECTOR_DE_IMAGES_TXT --offline`. Conserve ambas imágenes y paquetes
para rollback. El paquete genérico se importa en un directorio nuevo para no sobrescribir
configuración; revise los cambios de Compose/scripts antes de sustituirlos en la instalación.

## Diagnóstico

`diagnose.sh` usa SELECT 1, readiness HTTPS y TCP connect sin leer/escribir tags ni hacer
backfill. Un TCP disponible no prueba calidad ni permisos de lectura PLC. Compruebe rutas,
DNS/NTP, IPs por NIC, CA y token/registro de tags central. No desactive TLS para resolver
un error de certificados. Un HTTP 401/403 o tag desconocido deja failed que exige intervención.
No cambie IDs para resolver un rechazo.

Si PostgreSQL reinicia, el proceso puede salir por pérdida de conexión; Docker lo reinicia
y los leases se recuperan al vencer. Si un proceso permanece vivo pero unhealthy, capture
estado/logs y haga `sudo docker compose restart collector` tras diagnóstico. Compruebe
después tags insertados y pendientes, no solo proceso running.

## Pruebas reproducibles

Las pruebas sintéticas usan únicamente red interna Docker, simulador y central HTTPS
falso, con su propia BD/volumen efímero y token de pruebas. No montan runtime de planta.
Desde una estación de desarrollo Linux con Docker y OpenSSL:

```bash
docker build --platform linux/amd64 -t collector-offline:test .
mkdir -p test-results
set -o pipefail
bash scripts/test-docker.sh 2>&1 | tee test-results/docker.log
# Opcional, solo estación aislada con recursos suficientes:
bash scripts/test-docker.sh --volume-500k 2>&1 | tee test-results/volume-500k.log
```

El script prueba inicialización, ONLINE sin captura, caída central, lectura simulada,
persistencia de minutos/eventos/tag state, lease interrumpido, restart/recreación con
volumen, recuperación, pérdida de ACK/reintento/duplicate y conteos locales/centrales.
Solo elimina el volumen del proyecto único `collector-test-*` creado por la prueba.
La prueba opcional exige 4 GiB RAM disponible/8 GiB disco libre y procesa 500.000 filas,
registra tiempos de inserción/envío, tamaño BD y estadísticas de contenedores. No se
ejecutó en esta tarea. Mida también la capacidad del almacenamiento Docker, que puede
estar en un filesystem distinto al checkout.

La integración PostgreSQL del proyecto usa TEST_DATABASE_URL exclusivamente de una BD
desechable. No reutilice un URL de planta para pytest. Resultados y pendientes actuales:
[docs/RESULTADOS_PRUEBAS.md](docs/RESULTADOS_PRUEBAS.md).
