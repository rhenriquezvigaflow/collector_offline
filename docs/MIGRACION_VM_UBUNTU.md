# Migración de Collector Offline a una VM Ubuntu 24.04

Esta guía instala el Collector Offline en una VM Linux ubicada en la planta. La VM mantendrá
una copia local de la configuración de la laguna y una base PostgreSQL independiente llamada
`crystal_offline`, propiedad del usuario/rol local `collector_local`.

La instalación no reemplaza ni modifica el Collector Central. En estado `ONLINE`, el nodo
offline solo comprueba la disponibilidad del backend y no consulta el PLC.

## 1. Datos del despliegue

Complete estos valores antes de comenzar:

| Dato | Valor para `small_sim` | Producción |
|---|---|---|
| Sistema operativo | Ubuntu Server 24.04 LTS | `________________` |
| `source_node_id` | `edge-small-sim-01` | `________________` |
| `lagoon_id` | `small_sim` | `________________` |
| Producto | `small` | `crystal` o `small` |
| IP OPC UA | `192.168.100.10` | `________________` |
| Puerto OPC UA | `4840/TCP` | `________________` |
| URL central | `https://servidor/api` | `________________` |
| IP de la VM | — | `________________` |

## 2. Requisitos de red

La VM debe poder comunicarse con:

- El PLC o servidor OPC UA indicado en el YAML local.
- El backend central mediante HTTPS.
- DNS y NTP corporativos.
- Los repositorios de Ubuntu y Python durante la instalación.

No es necesario publicar PostgreSQL ni la API operativa del Collector Offline. Ambos quedan
limitados a la VM.

Compruebe desde Ubuntu:

```bash
ip address
ip route
timedatectl
getent hosts backend-central.example.com
nc -vz 192.168.100.10 4840
curl --fail --show-error https://backend-central.example.com/api/health/ready
```

El último comando debe responder con `status: ready` y `checks.database: ok`. Un ping exitoso
no es suficiente para declarar disponible el servicio central.

Si la empresa utiliza una CA privada, copie su certificado a la VM y configure después
`central.ca_bundle` con su ruta absoluta.

## 3. Empaquetar el proyecto en Windows

Ejecute en PowerShell desde `C:\WebCrystalScada\collector-offline`:

```powershell
Set-Location C:\WebCrystalScada\collector-offline

tar.exe -czf C:\Temp\collector-offline.tar.gz `
  --exclude=.venv `
  --exclude=.pytest_cache `
  --exclude=.mypy_cache `
  --exclude=.ruff_cache `
  --exclude=__pycache__ `
  .
```

Copie el archivo a la VM:

```powershell
scp C:\Temp\collector-offline.tar.gz usuario-admin@IP_DE_LA_VM:/tmp/
```

También puede transferirse mediante SFTP o el mecanismo corporativo aprobado. No incluya
archivos `.env`, tokens, respaldos de base de datos ni el entorno virtual de Windows.

## 4. Preparar el contenido en Ubuntu

Conéctese a la VM:

```bash
ssh usuario-admin@IP_DE_LA_VM
```

Extraiga una copia de instalación temporal:

```bash
sudo install -d -m 0750 /tmp/collector-offline-install
sudo tar -xzf /tmp/collector-offline.tar.gz -C /tmp/collector-offline-install
sudo chown -R "$USER":"$USER" /tmp/collector-offline-install
cd /tmp/collector-offline-install
```

Verifique que la copia local de la laguna exista:

```bash
test -f config/collector-offline.yml
test -f config/small_sim.yml
grep -n 'include: "small_sim.yml"' config/collector-offline.yml
```

El resultado esperado del último comando es:

```yaml
plcs:
  - include: "small_sim.yml"
```

Si los archivos se transfirieron mediante un método que convirtió finales de línea, normalice
solo los scripts:

```bash
sed -i 's/\r$//' scripts/*.sh
chmod 0750 scripts/*.sh
bash -n scripts/install.sh scripts/provision-postgres.sh scripts/smoke-test.sh
```

## 5. Configurar la laguna

Para `small_sim`, revise `config/small_sim.yml` y confirme:

```yaml
lagoon_id: "small_sim"
product_type: "small"
source: siemens

opcua_modules:
  - id: "recirculation_pump"
    opc_server_url: "opc.tcp://192.168.100.10:4840"
```

El PLC se selecciona con `source`, usando el mismo contrato del Collector Central:

- `source: siemens` requiere `siemens.opc_server_url` con `tags`, o bien `opcua_modules`.
- `source: rockwell` requiere `rockwell.ip`, `rockwell.slot` y `tags`.

Ejemplo Rockwell disponible en `config/rockwell-example.yml`:

```yaml
source: rockwell
force_reconnect_every_sec: 3600
max_consecutive_fails: 10
rockwell:
  ip: "192.168.26.10"
  slot: 0
  timeout_sec: 5
tags:
  PT203_R: "PT203_R"
  P300_ST: "P300_ST"
```

Copie el YAML oficial de la laguna desde el Collector Central al paquete offline y manténgalo
local. No cambie solamente `source`: las direcciones Siemens OPC UA y los tags Rockwell usan
contratos distintos y deben corresponder al PLC seleccionado.

Las direcciones y tags deben existir localmente en este archivo. No deje un `include` que
apunte al repositorio del Collector Central ni a una unidad de red.

Revise también `config/collector-offline.yml`:

```yaml
node:
  source_node_id: "edge-small-sim-01"
  lagoon_id: "small_sim"
  product_type: "small"

central:
  health_url: "https://backend-central.example.com/api/health/ready"
  sync_url: "https://backend-central.example.com/api/ingest/offline/v1/backfill"
  verify_tls: true

plcs:
  - include: "small_sim.yml"
```

No configure `/ingest/scada` como `sync_url`; ese endpoint pertenece al flujo realtime.

## 6. Preparar el backend central

Este paso se ejecuta una sola vez en el servidor central y requiere permisos de propietario de
la base de datos.

Desde el repositorio `crystal-backend` del servidor central:

```bash
psql "$CENTRAL_DATABASE_URL" \
  -v ON_ERROR_STOP=1 \
  -f scripts/create_offline_backfill.sql
```

Genere un token distinto para el nodo:

```bash
umask 077
TOKEN="$(openssl rand -hex 32)"
TOKEN_HASH="$(printf %s "$TOKEN" | sha256sum | awk '{print $1}')"
```

Registre el nodo autorizado. El token plano no se guarda en la base central:

```bash
psql "$CENTRAL_DATABASE_URL" \
  --set=ON_ERROR_STOP=1 \
  --set=source_node_id='edge-small-sim-01' \
  --set=lagoon_id='small_sim' \
  --set=token_hash="$TOKEN_HASH" <<'SQL'
INSERT INTO public.offline_collector_node (
    source_node_id,
    lagoon_id,
    token_hash,
    enabled
)
VALUES (
    :'source_node_id',
    :'lagoon_id',
    :'token_hash',
    true
)
ON CONFLICT (source_node_id) DO UPDATE
SET lagoon_id = EXCLUDED.lagoon_id,
    token_hash = EXCLUDED.token_hash,
    enabled = true,
    updated_at = now();
SQL
```

Mantenga la terminal abierta hasta guardar `$TOKEN` en la VM. No lo envíe por correo, chat ni
lo agregue a Git.

## 7. Ejecutar el instalador en la VM

Desde `/tmp/collector-offline-install`:

```bash
sudo bash scripts/install.sh
```

El instalador realiza las siguientes operaciones:

1. Instala Python, PostgreSQL local, certificados y herramientas requeridas.
2. Crea el usuario de sistema sin privilegios `collector_local`.
3. Copia la aplicación a `/opt/crystal/collector-offline`.
4. Copia todos los YAML locales nuevos a `/etc/crystal/collector-offline`, sin sobrescribir los
   que ya estén configurados en la VM.
5. Crea el rol `collector_local` y la base PostgreSQL independiente `crystal_offline`.
6. Crea el entorno virtual e instala drivers Siemens/Rockwell.
7. Aplica las migraciones locales.
8. Instala y habilita el servicio systemd, pero no lo inicia.

Compruebe la base separada:

```bash
sudo -u postgres psql -d crystal_offline -c '\dt'
sudo -u collector_local psql -d crystal_offline -c \
  'SELECT id, state FROM edge_runtime_state;'
```

La segunda consulta debe devolver la fila `1 | online`.

## 8. Instalar el token y URLs definitivas

Edite el archivo protegido:

```bash
sudoedit /etc/crystal/collector-offline.env
```

Contenido esperado:

```dotenv
COLLECTOR_OFFLINE_DATABASE_URL=postgresql+psycopg2:///crystal_offline
COLLECTOR_OFFLINE_TOKEN=PEGAR_AQUI_EL_TOKEN_GENERADO
```

Compruebe permisos y elimine el token de la sesión de administración:

```bash
sudo chown root:collector_local /etc/crystal/collector-offline.env
sudo chmod 0600 /etc/crystal/collector-offline.env
unset TOKEN TOKEN_HASH
```

Copie las URLs definitivas y, si corresponde, la CA privada en:

```bash
sudoedit /etc/crystal/collector-offline/collector-offline.yml
```

Valide la configuración sin iniciar captura:

```bash
sudo -u collector_local bash -c '
  set -a
  source /etc/crystal/collector-offline.env
  set +a
  /opt/crystal/collector-offline/.venv/bin/python -c \
    "from collector_offline.config import load_config; c,_ = load_config(\"/etc/crystal/collector-offline/collector-offline.yml\"); print(c.node, c.plcs[0].source)"
'
```

## 9. Iniciar el servicio

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now collector-offline.service
sudo systemctl status --no-pager --full collector-offline.service
```

Revise los logs JSON:

```bash
sudo journalctl -u collector-offline.service -n 100 --no-pager -o cat
sudo journalctl -u collector-offline.service -f -o cat
```

Compruebe FastAPI local:

```bash
curl --fail --silent http://127.0.0.1:9187/health/live | jq
curl --fail --silent http://127.0.0.1:9187/health/ready | jq
curl --fail --silent http://127.0.0.1:9187/status | jq
```

Si `jq` no está instalado, quite `| jq`.

Con el backend central disponible, el estado esperado es `online`, sin backlog y sin lecturas
locales intencionales.

## 10. Validar aislamiento y seguridad

Compruebe que PostgreSQL y FastAPI no estén publicados:

```bash
sudo ss -lntp | grep -E '(:5432|:9187)'
```

El puerto 9187 debe escuchar únicamente en `127.0.0.1`. PostgreSQL debe usar socket Unix o
loopback.

Reglas UFW de referencia — aplíquelas únicamente después de confirmar acceso SSH, DNS y NTP:

```bash
sudo ufw default deny incoming
sudo ufw allow OpenSSH
sudo ufw allow out to 192.168.100.10 port 4840 proto tcp
sudo ufw allow out 443/tcp
sudo ufw enable
```

No abra 5432 ni 9187 hacia la red de planta.

## 11. Prueba controlada de failover

Realice esta prueba en una ventana aprobada. No interrumpa el backend o red de producción sin
autorización. Es preferible usar un backend de staging o bloquear temporalmente el destino solo
desde la VM.

1. Confirme estado inicial:

   ```bash
   curl -s http://127.0.0.1:9187/status | jq
   ```

2. Interrumpa de forma controlada la ruta al backend central.
3. Después de uno y dos health checks fallidos, el estado debe continuar `online`.
4. Al tercer fallo consecutivo debe cambiar a `offline` y crear una única sesión.
5. Espere al menos dos minutos y compruebe captura:

   ```bash
   sudo -u collector_local psql -d crystal_offline <<'SQL'
SELECT id, status, started_at
FROM local_outage_session
ORDER BY started_at DESC
LIMIT 3;

SELECT lagoon_id, tag_id, bucket, state, value_num, value_bool
FROM local_scada_minute
ORDER BY bucket DESC, tag_id
LIMIT 30;
SQL
   ```

6. Restaure la conexión central.
7. El nodo debe detener la captura, pasar a `syncing`, vaciar el backlog y volver a `online`.
8. Compruebe que no quedan errores:

   ```bash
   curl -s http://127.0.0.1:9187/status | jq

   sudo -u collector_local psql -d crystal_offline <<'SQL'
SELECT sync_status, count(*)
FROM local_scada_minute
GROUP BY sync_status;

SELECT sync_status, count(*)
FROM local_scada_event
GROUP BY sync_status;
SQL
   ```

Los registros confirmados deben aparecer como `sent`. Los datos históricos enviados no deben
generar WebSockets, notificaciones ni alarmas realtime en el backend central.

## 12. Actualización futura

Antes de instalar una nueva versión:

```bash
sudo systemctl stop collector-offline
sudo pg_dump -Fc crystal_offline \
  -f /var/backups/crystal_offline-"$(date +%F-%H%M)".dump
sudo cp -a /etc/crystal/collector-offline \
  /var/backups/collector-offline-config-"$(date +%F-%H%M)"
sudo cp -a /etc/crystal/collector-offline.env \
  /var/backups/collector-offline.env-"$(date +%F-%H%M)"
```

Transfiera y extraiga la nueva versión, ejecute nuevamente `scripts/install.sh`, revise los YAML
preservados y luego:

```bash
sudo systemctl restart collector-offline
sudo journalctl -u collector-offline -n 100 --no-pager
```

Cuando cambien tags en el Collector Central, copie la versión aprobada del YAML a la VM. Nunca
reemplace el YAML local por una referencia remota.

## 13. Rollback

Si el servicio no inicia o la configuración no es válida:

```bash
sudo systemctl stop collector-offline
sudo systemctl disable collector-offline
```

El Collector Central continúa operando como fuente principal. Detener el nodo offline no exige
revertir el backend realtime.

Para restaurar una versión anterior:

1. Restaure `/opt/crystal/collector-offline` desde el paquete anterior.
2. Restaure los YAML y el archivo de entorno desde `/var/backups`.
3. No restaure la base si contiene backlog `pending`, `syncing` o `failed` más reciente.
4. Ejecute migraciones y vuelva a iniciar systemd.

No elimine manualmente backlog pendiente para resolver un problema de despliegue.

## 14. Troubleshooting

### El servicio no inicia

```bash
sudo systemctl status collector-offline --full
sudo journalctl -u collector-offline -n 200 --no-pager
sudo systemctl cat collector-offline
```

### Error de configuración YAML

Confirme que `small_sim.yml` esté junto a `collector-offline.yml` en `/etc/crystal/collector-offline`
y que el `include` sea relativo:

```bash
sudo -u collector_local test -r /etc/crystal/collector-offline/small_sim.yml
```

### No hay lecturas OPC UA

```bash
nc -vz 192.168.100.10 4840
sudo journalctl -u collector-offline -f -o cat
```

Compruebe IP, namespace IDs, firewall, credenciales OPC UA y el paquete opcional `plc`.

### Backlog `failed`

Corrija primero token, autorización o tag. Luego reencole explícitamente:

```bash
sudo -u collector_local bash -c '
  set -a
  source /etc/crystal/collector-offline.env
  set +a
  /opt/crystal/collector-offline/.venv/bin/collector-offline retry-failed \
    --config /etc/crystal/collector-offline/collector-offline.yml
'
```

### Segunda instancia rechazada

El advisory lock está funcionando. No ejecute manualmente otro proceso si systemd ya mantiene
uno activo:

```bash
pgrep -af collector-offline
sudo systemctl status collector-offline
```

## 15. Checklist de aceptación

- [ ] La VM alcanza el PLC y el readiness central.
- [ ] `small_sim.yml` existe localmente y no contiene referencias remotas.
- [ ] `source` y su bloque `siemens` o `rockwell` corresponden al PLC de la laguna.
- [ ] La base separada `crystal_offline` pertenece a `collector_local` y no publica 5432.
- [ ] El token corresponde al `source_node_id` y `lagoon_id` registrados centralmente.
- [ ] systemd está habilitado y reinicia automáticamente.
- [ ] FastAPI local escucha solamente en loopback.
- [ ] En `ONLINE` no existen lecturas PLC locales intencionales.
- [ ] El tercer fallo activa una única sesión `OFFLINE`.
- [ ] Los buckets se almacenan una sola vez y en UTC.
- [ ] Al recuperar conexión, la captura se detiene antes de `SYNCING`.
- [ ] El backlog termina en `sent` solo tras acuse central.
- [ ] El backfill no genera alarmas, notificaciones ni WebSockets realtime.
- [ ] Se documentaron respaldo, rollback y responsable operativo.
