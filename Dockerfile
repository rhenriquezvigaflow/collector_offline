# syntax=docker/dockerfile:1
ARG PYTHON_IMAGE=python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df
FROM ${PYTHON_IMAGE} AS build
WORKDIR /build
COPY requirements.lock requirements-build.lock ./
RUN pip install --no-cache-dir --require-hashes -r requirements-build.lock \
    && pip install --no-cache-dir --require-hashes --no-build-isolation --prefix=/install -r requirements.lock
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir --no-deps --no-build-isolation --prefix=/install .

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 TZ=UTC \
    COLLECTOR_CONFIG=/etc/collector/site.yml
RUN groupadd --gid 10001 collector \
    && useradd --uid 10001 --gid collector --no-create-home --shell /usr/sbin/nologin collector
COPY --from=build /install /usr/local
WORKDIR /opt/collector
COPY migrations/local ./migrations/local
USER 10001:10001
STOPSIGNAL SIGTERM
ENTRYPOINT ["python", "-m", "collector_offline.container"]
CMD ["run"]
HEALTHCHECK --interval=30s --timeout=15s --start-period=90s --retries=3 \
    CMD ["python", "-m", "collector_offline.container", "healthcheck"]
