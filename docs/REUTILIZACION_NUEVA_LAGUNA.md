# Reutilización en una nueva laguna

## Objetivo

El código, las migraciones, el servicio systemd y la estructura de PostgreSQL son comunes a
todas las instalaciones. La única configuración funcional que cambia por laguna es:

```text
/etc/crystal/collector-offline/site.yml
```

El archivo `.env` no configura la laguna: contiene únicamente secretos de la instalación. El
token se provisiona una sola vez y no debe guardarse en Git ni dentro del YAML.

## Qué resuelve automáticamente el programa

A partir de `site.yml`, el Collector Offline:

- deriva `source_node_id` como `edge-<lagoon_id-normalizado>-01`;
- selecciona Rockwell, Siemens o simulador mediante `source`;
- crea el lector adecuado usando la conexión y los tags del YAML;
- comprueba el backend central cada 30 segundos;
- abre una sesión y captura localmente cuando confirma la caída;
- mantiene los datos en la base común `crystal_offline`;
- prepara los mismos campos requeridos por `scada_minute` y `scada_event`;
- sincroniza el backlog al recuperar el backend;
- publica su estado local por FastAPI en `127.0.0.1:9187`;
- aplica migraciones pendientes al iniciar.

## Procedimiento para un servidor nuevo

### 1. Preparar solamente `site.yml`

Copie el YAML oficial del Collector Central:

```bash
cp /ruta/al/yaml-oficial.yml config/site.yml
```

Al final agregue:

```yaml
offline:
  central:
    base_url: "https://host-del-backend-central/api"
```

Si el backend central aún usa HTTP dentro de una red privada:

```yaml
offline:
  central:
    base_url: "http://192.168.2.100:8090"
    allow_private_http: true
```

No se admite HTTP público. Para una CA privada se puede agregar `ca_bundle`. Nunca use
`verify_tls: false`.

### 2. Validar antes de instalar

```bash
collector-offline validate --config config/site.yml
```

El comando no necesita la base ni el token. Debe mostrar `valid: true`, la laguna, la
identidad derivada, la fuente del PLC y la cantidad de tags.

### 3. Instalar

```bash
sudo bash scripts/install.sh --site config/site.yml
```

El instalador:

- instala Python, PostgreSQL y dependencias;
- crea el usuario de servicio y rol local `collector_local`;
- crea la base separada `crystal_offline`;
- copia el YAML a `/etc/crystal/collector-offline/site.yml`;
- valida la configuración y aplica las migraciones;
- instala y habilita `collector-offline.service`.

Si el archivo preparado tiene otro nombre:

```bash
sudo bash scripts/install.sh --site /ruta/laguna-norte.yml
```

El instalador es idempotente y no sobrescribe un `site.yml` que ya existe en `/etc`.

### 4. Provisionar el secreto

Genere un token distinto para cada nodo:

```bash
openssl rand -hex 32
```

Registre en el backend central:

- el `source_node_id` mostrado por `validate`;
- el `lagoon_id`;
- el hash SHA-256 del token.

Guarde el token plano solamente en:

```bash
sudoedit /etc/crystal/collector-offline.env
```

### 5. Iniciar y comprobar

```bash
sudo systemctl restart collector-offline
sudo systemctl status collector-offline --no-pager
sudo journalctl -u collector-offline -f -o cat
curl -fsS http://127.0.0.1:9187/status
```

## Cambio posterior de laguna, PLC o tags

En el servidor:

```bash
sudoedit /etc/crystal/collector-offline/site.yml
sudo -u collector_local \
  /opt/crystal/collector-offline/.venv/bin/collector-offline validate \
  --config /etc/crystal/collector-offline/site.yml
sudo systemctl restart collector-offline
```

No edite archivos Python. Tampoco cambie `initial.sql`, systemd ni los scripts para agregar
una laguna.

## Rockwell

```yaml
lagoon_id: "laguna_rockwell"
product_type: "crystal"
source: rockwell

rockwell:
  ip: "192.168.20.10"
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
    base_url: "https://central.example/api"
```

Las mayúsculas son significativas para el nombre lógico y para la dirección del PLC. Por eso
`Motor` y `motor` no se consideran el mismo tag.

## Siemens por OPC UA

```yaml
lagoon_id: "laguna_siemens"
product_type: "crystal"
source: siemens

siemens:
  opc_server_url: "opc.tcp://192.168.30.10:4840"
  timeout_sec: 5

tags:
  PT117_R_SCADA: "ns=4;i=3"
  P001_ST: "ns=4;i=23"

event_tags:
  P001_ST: "Bomba de recirculación"

offline:
  central:
    base_url: "https://central.example/api"
```

También se acepta la estructura `opcua_modules` utilizada por el Collector Central.

## Opciones offline no habituales

Los valores por defecto permiten que normalmente solo se configure `offline.central`. Si una
planta requiere ajustes operativos, pueden permanecer en el mismo YAML:

```yaml
offline:
  source_node_id: "edge-identidad-registrada-02"
  central:
    base_url: "https://central.example/api"
    timeout_sec: 10
  supervisor:
    health_interval_sec: 30
    failure_threshold: 3
    recovery_threshold: 2
  capture:
    interval_sec: 60
  sync:
    batch_size: 25
  retention:
    enabled: true
    days: 90
```

## Compatibilidad con instalaciones antiguas

El cargador aún admite `collector-offline.yml` con `plcs[].include`. Esa compatibilidad evita
romper nodos desplegados, pero una instalación nueva debe usar `site.yml`. Una migración
controlada consiste en:

1. copiar el contenido del YAML incluido;
2. agregarle el bloque `offline`;
3. validarlo;
4. cambiar systemd para apuntar a `site.yml`;
5. reiniciar y revisar logs.

## Límite de “solo editar YAML”

La selección de laguna, PLC, direcciones, tags y parámetros operativos requiere únicamente el
YAML. Hay dos acciones externas inevitables y deliberadamente separadas:

- asignar un token secreto al nodo;
- registrar la identidad y el hash de ese token en el backend central.

Esto no es hardcode de la laguna: es el control de acceso que impide que un servidor suplante
a otro.
