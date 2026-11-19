# Resultados de validación

Fecha: 19 de noviembre de 2026. Rama: `feat/docker-minipc`.
Entorno: Windows, Python 3.12.16 instalado en entorno de pruebas separado. Docker Engine
no está instalado/disponible; WSL no ofrece un Linux utilizable. No se instaló un motor,
no se arrancó una BD de planta, no se conectó a un PLC ni se enviaron datos a un central real.

## Verificado localmente

| Verificación | Resultado |
|---|---|
| pytest, suite original y ampliaciones | 40 passed, 1 skipped |
| Integración omitida | PostgreSQL real: falta TEST_DATABASE_URL desechable |
| Ruff, src/tests/docker | Sin errores |
| mypy src | Sin errores, 24 archivos fuente |
| Sintaxis Bash | Scripts nuevos y de prueba aceptados por bash -n de Git Bash |
| Configuración Compose operativa y de pruebas | Aceptada por Docker Compose CLI v5.8.0, sin Engine |
| Integridad del CLI usado | SHA256 de release oficial verificado |
| Dependencias de ejecución | Un aviso residual en opcua, informe JSON adjunto |
| Referencias de imágenes base | Digests consultados en Docker Registry oficial |

La suite nueva valida secretos por archivo, exclusión variable/_FILE, URL encoding de
password PostgreSQL, errores sin eco de token, zona IANA, límite de tag_id, configuración
sin conexión y redacción de logs. Una prueba abre un HTTP falso exclusivamente en
127.0.0.1 con puerto efímero: usa el cliente real requests y SyncWorker para simular
commit central seguido de 503, y confirma reenvío idéntico, ACK duplicate y cierre ONLINE.
No transmite filas sintéticas a un destino externo. Las pruebas existentes cubren
microcortes, no lectura ONLINE, captura por bucket, eventos/cierre, reinicios lógicos,
leases, rechazo parcial/permanente y retención sobre datos desechables SQLite.

La prueba de distribución ejecuta export-usb.sh con Docker simulado y examina el tar
real generado: no contiene runtime/configuración de planta, .env ni clave privada
centinela; conserva archivos operativos y aliases de imágenes en .env.usb. No demuestra
un docker save/load real.

Existe un aviso de deprecación del adaptador httpx de TestClient, sin fallos de pruebas.
SQLite no verifica índices parciales, SKIP LOCKED ni transacciones PostgreSQL reales.
La validación Compose no equivale a build, arranque o salud observada.

## Pendiente de ejecución en Docker Linux

1. `docker build --platform linux/amd64 -t collector-offline:test .`.
2. `bash scripts/test-docker.sh`: inicialización con rol no superusuario, salud,
   contingencia, datos persistentes, restart/recreación PostgreSQL/Collector, leases,
   dump/restore transaccional en otra BD desechable, reintentos/ACK duplicado, conteos
   y reinicio automático después de SIGKILL.
3. Restauración de dump en un volumen desechable nuevo y comprobación de constraints/roles.
4. Reinicio del mini PC/corte de energía controlado; verificar recuperación sin escritorio.
5. Instalador APT, permisos UID 10001 y operación con NetworkManager en Ubuntu Desktop.
6. Exportación/importación real docker save/load desde USB, update y rollback con datos
   pendientes en instalación aislada.
7. PLC real: tags válidos y filas insertadas durante contingencia, rutas y timeouts.
8. Contrato central real: autorización de nodo/laguna/tag, origen collector_offline,
   idempotencia y ausencia de WS/notificaciones sobre histórico.
9. Prueba opcional 500.000 filas con métricas: preparada pero no ejecutada.

Estas pruebas no se declaran exitosas. El workflow manual prepara build/test antes de
publicar, pero no se ha disparado ni publicado. No hay push ni despliegues.

## Comandos usados

```powershell
.\.venv-test\Scripts\python.exe -m pytest -q
.\.venv-test\Scripts\ruff.exe check src tests docker
.\.venv-test\Scripts\mypy.exe src
.\.venv-docker\tools\docker-compose-windows-x86_64.exe --env-file .env.example -f compose.yml config --quiet
# TEST_TLS_DIR temporal para resolver el bind en la validación del stack desechable:
.\.venv-docker\tools\docker-compose-windows-x86_64.exe -f tests/docker/compose.yml config --quiet
.\.venv-test\Scripts\pip-audit.exe -r requirements.lock --no-deps --disable-pip
```

La auditoría de dependencias final sin excepciones sale con código 1 por el aviso
OPC UA documentado; con la excepción específica CVE-2022-25304 no debe haber otros avisos.
