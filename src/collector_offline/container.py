"""Container adapter; leaves capture, transitions and the backfill contract unchanged."""

from __future__ import annotations

import json
import os
import socket
import sys
from contextlib import suppress
from urllib.parse import urlsplit
from urllib.request import urlopen

from sqlalchemy import URL, text

from collector_offline import cli
from collector_offline.config import load_config, secret_from_env
from collector_offline.infrastructure.database import build_engine
from collector_offline.infrastructure.http_client import CentralClient


def configure_database() -> None:
    if os.environ.get("COLLECTOR_OFFLINE_DATABASE_URL"):
        return
    password = secret_from_env("DATABASE_PASSWORD")
    if not password:
        raise ValueError("DATABASE_PASSWORD_FILE is missing or empty")
    url = URL.create(
        "postgresql+psycopg2",
        username=os.environ.get("APP_DB_USER", "collector_local"),
        password=password,
        host=os.environ.get("POSTGRES_HOST", "postgres"),
        port=int(os.environ.get("POSTGRES_PORT", "5432")),
        database=os.environ.get("POSTGRES_DB", "crystal_offline"),
    )
    os.environ["COLLECTOR_OFFLINE_DATABASE_URL"] = url.render_as_string(hide_password=False)


def validate(config_path: str) -> None:
    config, _ = load_config(config_path)
    if not config.status_server.enabled:
        raise ValueError("Docker requires status_server.enabled=true for healthchecks")
    print(
        json.dumps(
            {
                "configuration": "valid",
                "source_node_id": config.node.source_node_id,
                "lagoon_id": config.node.lagoon_id,
                "plcs": len(config.plcs),
            }
        )
    )


def healthcheck(config_path: str) -> None:
    config, _ = load_config(config_path)
    host = config.status_server.host
    host = f"[{host}]" if ":" in host else host
    with urlopen(f"http://{host}:{config.status_server.port}/health/ready", timeout=10) as response:
        payload = json.load(response)
    if payload.get("status") != "ready":
        raise ValueError("local readiness is degraded")
    print(json.dumps(payload))


def diagnose(config_path: str) -> int:
    """SELECT 1, central readiness GET and TCP connect only; no PLC reads or sync."""
    config, secrets = load_config(config_path)
    checks: list[dict[str, object]] = []
    engine = build_engine(secrets.database_url, config.database)
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        checks.append({"component": "database", "ok": True})
    except Exception as exc:
        checks.append({"component": "database", "ok": False, "error": type(exc).__name__})
    finally:
        engine.dispose()
    client = CentralClient(
        config.central,
        secrets.central_token,
        config.node.source_node_id,
        config.node.lagoon_id,
        config.node.product_type,
    )
    try:
        result = client.check_health()
        checks.append(
            {"component": "central", "ok": result.available, "http_status": result.status_code}
        )
    finally:
        client.close()
    for plc in config.plcs:
        endpoints: list[tuple[str, int]] = []
        if plc.source == "rockwell" and plc.rockwell:
            endpoints.append((str(plc.rockwell.ip), 44818))
        if plc.source == "siemens":
            urls = [m.opc_server_url or f"opc.tcp://{m.ip}:4840" for m in plc.opcua_modules]
            if not urls and plc.siemens:
                urls = [str(plc.siemens.opc_server_url)]
            for url in urls:
                parsed = urlsplit(url)
                endpoints.append((str(parsed.hostname), parsed.port or 4840))
        for host, port in endpoints:
            try:
                with socket.create_connection((host, port), timeout=5):
                    pass
                checks.append({"component": plc.name, "port": port, "ok": True})
            except OSError as exc:
                checks.append(
                    {"component": plc.name, "port": port, "ok": False, "error": type(exc).__name__}
                )
    print(json.dumps({"checks": checks}))
    return 0 if all(c["ok"] for c in checks) else 1


def main() -> int:
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    config_path = os.environ.get("COLLECTOR_CONFIG", "/etc/collector/site.yml")
    try:
        configure_database()
        if command == "validate":
            validate(config_path)
        elif command == "healthcheck":
            healthcheck(config_path)
        elif command == "diagnose":
            return diagnose(config_path)
        elif command in {"run", "migrate", "status", "retry-failed"}:
            validate(config_path)
            sys.argv = ["collector-offline", command, "--config", config_path]
            return cli.main()
        else:
            raise ValueError("unknown command")
    except Exception as exc:
        # Runtime exceptions can contain DSNs or URLs. Never print credentials.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        for name in (
            "DATABASE_PASSWORD",
            "COLLECTOR_OFFLINE_TOKEN",
            "COLLECTOR_OFFLINE_PLC_PASSWORD",
        ):
            with suppress(ValueError):
                value = secret_from_env(name)
                if value:
                    message = message.replace(value, "[redacted]")
        print(f"Collector startup/check failed: {message}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
