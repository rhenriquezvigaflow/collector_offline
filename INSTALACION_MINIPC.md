# Instalación en mini PC industrial

Destino inicial: Ubuntu Desktop 24.04 LTS `amd64`, Docker Engine y Compose plugin.
La aplicación funciona con Docker habilitado en el arranque y no requiere abrir sesión gráfica.
Use una instalación por nodo/laguna; no reutilice una BD de otra planta cambiando el YAML.

## Preparar Ubuntu y red

Instale Ubuntu Desktop 24.04 LTS, actualizaciones aprobadas, sincronización NTP y acceso SSH
administrativo por clave. Referencia inicial de dimensionamiento, pendiente de medición en
hardware real: 2 núcleos, 4 GiB RAM, SSD industrial de 64 GiB o más, margen para WAL y al
menos dos respaldos. Para 50 tags a un minuto, dimensione varios millones de filas por mes
y compruebe crecimiento real. Se recomienda UPS y retorno automático tras energía en BIOS.
No es una capacidad certificada para cualquier backlog.

Para habilitar SSH si se requiere administración remota y hay repositorios disponibles:

```bash
sudo apt-get update
sudo apt-get install -y openssh-server
sudo systemctl enable --now ssh.service
```

Instale la clave pública autorizada en `~/.ssh/authorized_keys` del usuario administrativo
con permisos 0700 para .ssh y 0600 para authorized_keys. Restrinja el acceso a la red de
administración/VPN y verifique una sesión por clave antes de endurecer sshd o el firewall.
No distribuya la clave privada junto con el Collector.

En Ubuntu Desktop use Configuración → Red o `nm-connection-editor` para configurar la IP
estática. Registre IP/prefijo, gateway, DNS y nombre de conexión. No configure interfaces
físicas desde Docker. Si hay dos NIC, la interfaz PLC debe tener IP de su subred y normalmente
no llevar gateway por defecto; la interfaz de administración/central lleva gateway y DNS.

```bash
nmcli connection show
ip -br address
ip route
timedatectl
```

Ejemplo administrativo, adaptando nombres y direcciones antes de aplicarlo desde consola
local (cambiar la conexión usada por SSH puede desconectarlo):

```bash
sudo nmcli connection modify 'PLC' ipv4.method manual \
  ipv4.addresses 192.168.100.20/24 ipv4.never-default yes
sudo nmcli connection modify 'CENTRAL' ipv4.method manual \
  ipv4.addresses 192.168.2.20/24 ipv4.gateway 192.168.2.1 \
  ipv4.dns '192.168.2.1' ipv4.never-default no
sudo nmcli connection up 'PLC'
sudo nmcli connection up 'CENTRAL'
```

No añada reglas genéricas que corten DNS, NTP o administración. Autorice salida unicast
al PLC: OPC UA usa el puerto del endpoint (normalmente 4840/TCP); Rockwell utiliza
44818/TCP con este driver. Verifique rutas desde el contenedor con el diagnóstico posterior.
El puerto Rockwell sigue el driver existente, no se inventa un nuevo campo de protocolo.

## Docker Engine

Siga el [procedimiento oficial para Ubuntu](https://docs.docker.com/engine/install/ubuntu/).
El instalador comprueba Ubuntu/arquitectura, y solo instala Docker mediante APT oficial al
recibir `--install-dependencies`. Si hay paquetes conflictivos docker.io/containerd/runc,
se detiene para que el administrador decida su migración; no los desinstala automáticamente.
Las versiones de paquetes APT son las del repositorio aprobado en ese momento; registre
`docker version` y `docker compose version` en la entrega de cada equipo.

```bash
sudo systemctl enable --now docker.service containerd.service
sudo docker version
sudo docker compose version
```

Los scripts usan sudo y no añaden usuarios al grupo docker (ese acceso equivale a root).
Para una planta sin Internet, aprovisione previamente Docker/Compose/python3 mediante
paquetes Ubuntu aprobados o una imagen de SO corporativa; `docker load` no instala el motor.

## Preparar versión y configuración

Obtenga la distribución desde el repositorio privado o un USB validado. Antes de crear
el repositorio remoto, revise las claves SSH que ya estaban versionadas; excluir del build
no sanea el historial. No se ejecutó push ni se publicó ninguna imagen durante esta tarea.

```bash
sudo install -d -m 0750 /opt/crystal/collector-offline
# Copie aquí el checkout/distribución aprobada, sin runtime de otra planta.
cd /opt/crystal/collector-offline
sudo cp .env.example .env
sudo chmod 600 .env
sudoedit .env
```

Defina `COLLECTOR_IMAGE` con la versión/digest real de GHCR; la referencia de ejemplo no
ha sido publicada. Ajuste TZ, límites y nombre de proyecto **una sola vez**. Cambiar
`COMPOSE_PROJECT_NAME` cambia el volumen que se monta y puede aparentar una BD vacía.
No cambie POSTGRES_DB/APP_DB_USER/passwords de una instalación existente sin una migración
administrativa: los init scripts solo se ejecutan en una base nueva.

Para GHCR privado use un token de solo lectura desde stdin en una sesión administrativa;
no coloque PAT en .env ni en scripts. Use credential helper o logout tras descargar:

```bash
printf '%s' "$GHCR_READ_TOKEN" | sudo docker login ghcr.io -u USUARIO --password-stdin
unset GHCR_READ_TOKEN
sudo docker compose pull
sudo docker logout ghcr.io
```

Prepare `/etc/crystal-site/site.yml` desde `site.example.yml`, complete nodo, laguna,
producto, URLs HTTPS y **tags reales aprobados**, protocolo, IP/slot o URL OPC UA y timezone.
El ejemplo es deliberadamente inválido hasta completar tags/IP. Los includes relativos
deben estar junto al YAML; `configure.sh` copia los .yml/.yaml del directorio proporcionado.
Guarde la CA corporativa, si aplica, en `runtime/config/central-ca.crt` con propietario
10001:10001/mode 0440 y configure `central.ca_bundle: /etc/collector/central-ca.crt`.
Las credenciales OPC UA usan los nombres COLLECTOR_OFFLINE_PLC_USERNAME/PASSWORD del ejemplo.
Si reutiliza otros nombres de variables, añada los correspondientes `_FILE` al Compose.

El token debe estar previamente autorizado para el mismo source_node_id/lagoon_id en el
servidor central, con los tag_id registrados según el contrato existente. La tarea no
realiza cambios en ese servidor. Guarde el token en un archivo protegido, no en una línea
de comandos ni en el YAML. Por ejemplo, el administrador puede cargarlo de su gestor de secretos
en `/root/collector-central-token`, modo 0600.

## Instalar y validar

Si Docker ya está instalado:

```bash
cd /opt/crystal/collector-offline
sudo bash install.sh --site /etc/crystal-site/site.yml \
  --token-file /root/collector-central-token
```

Para autorizar la instalación de Docker faltante, añada `--install-dependencies`.
Para usar otro archivo de parámetros en una instalación nueva:
`sudo bash install.sh --env /ruta/planta.env --site /ruta/site.yml --token-file /ruta/token`.
Con OPC UA autenticado, añada `--plc-username-file` y `--plc-password-file`.
La repetición preserva configuración y secretos ya creados; puede ejecutar
`sudo bash install.sh` posteriormente con .env/runtime completos.

El instalador descarga imágenes, valida configuración sin lecturas PLC/envíos, inicializa
PostgreSQL nuevo, ejecuta migraciones pendientes y espera salud. Después realiza SELECT 1,
GET readiness central y conexión TCP a puertos PLC, sin enviar escrituras ni leer tags.
La captura real solo puede activarse por la contingencia existente después de arrancar.
Salida 3 significa servicios iniciados con diagnóstico externo incompleto, no fallo total
ni prueba de captura. No declare lectura real solo por TCP disponible.

```bash
sudo bash status.sh
sudo bash diagnose.sh
sudo docker compose logs --tail=100 collector
sudo docker compose exec collector python -m collector_offline.container status
```

Valide hardware y conectividad en una ventana autorizada: estado ONLINE sin lecturas,
contingencia con tags válidos, recuperación, pendientes vaciados y reinicio del equipo.
Compruebe que no existe Collector systemd ni otro nodo Docker leyendo el mismo PLC;
el advisory lock solo protege instancias que comparten esta BD, no otras instalaciones.

## Construcción local sin publicar

En una estación Linux con Docker, para probar esta rama antes de disponer de GHCR:

```bash
docker build --platform linux/amd64 -t collector-offline:0.1.0-docker .
# Configure COLLECTOR_IMAGE=collector-offline:0.1.0-docker en .env
sudo bash install.sh --offline --site /ruta/site.yml --token-file /ruta/token
```

Importe/pull previamente la imagen PostgreSQL fijada en .env. El modo offline nunca hace
pull ni build. Las pruebas aisladas se ejecutan como explica OPERACION_MANTENIMIENTO.md.
