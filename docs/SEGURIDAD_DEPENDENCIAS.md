# Verificación de dependencias

Verificación realizada el 19 de noviembre de 2026 con Python 3.12.16 y pip-audit 2.10.1.
Los lockfiles fijan todas las versiones de aplicación y herramientas de build. Las imágenes
base Python y PostgreSQL están fijadas por digest verificado contra Docker Registry.
No se realizó un análisis de paquetes del sistema de la imagen construida: requiere Docker.

La primera resolución que conservaba FastAPI 0.128.x arrastraba Starlette 0.52.1 y varios
avisos conocidos. Se actualizó la API local a FastAPI 0.135.4 y Starlette 1.10.0; la suite
de regresión de estado/captura/sync y la API sigue pasando. No se modifica el transporte
de sincronización por requests ni los drivers. Se añadió tzdata para validar zonas IANA
en Linux y Windows. [Aviso de Starlette sobre formularios](https://github.com/Kludex/starlette/security/advisories/GHSA-82w8-qh3p-5jfq).

## Aviso residual del driver existente

`opcua==0.98.13` presenta **CVE-2022-25304 / GHSA-mfpj-3qhm-976m** (DoS por chunks sin
límite). pip-audit no ofrece versión corregida de ese paquete. Se conserva el driver que
usa el proyecto; cambiar a otro cliente OPC UA requiere una decisión y validación PLC
independiente. No se declara la dependencia libre de vulnerabilidades.
[Aviso publicado](https://github.com/advisories/GHSA-mfpj-3qhm-976m).

La aplicación actúa como cliente y no publica un servidor OPC UA. Se acotan memoria/CPU,
se mantiene persistencia transaccional y se requiere red de planta controlada con salida
limitada a destinos aprobados. Estas medidas limitan exposición/impacto, no corrigen el
parser ni prueban inmunidad ante un PLC/endpoint comprometido.

El workflow manual bloquea avisos nuevos y exceptúa **solo** CVE-2022-25304 de forma
explícita y trazable. El informe sin excepciones es
[dependency-audit.json](dependency-audit.json). Para repetir:

```bash
pip-audit -r requirements.lock --no-deps --disable-pip --format json --output audit.json
# Exit 1 esperado mientras exista el aviso residual.
pip-audit -r requirements.lock --no-deps --disable-pip --ignore-vuln CVE-2022-25304
```

Los hashes de las distribuciones Python están incluidos en requirements.lock y
requirements-build.lock; pip los exige durante la construcción. Actualice y revise los
locks y digests por versión, vuelva a auditar y pruebe antes de publicar. No se hacen
actualizaciones automáticas de dependencias al arrancar un mini PC.

Las claves SSH `rhenriquez*` ya estaban versionadas antes de esta tarea. Se retiraron del
índice de Git mediante `git rm --cached`, preservando ambos archivos locales. `.dockerignore`
y export-usb usan listas explícitas para no distribuirlas. El administrador debe revisar
su vigencia/rotación y el historial antes de publicar el repositorio, incluso privado.
Esta tarea no elimina esos archivos locales ni reescribe el historial Git; los commits
anteriores todavía contienen sus versiones históricas.
