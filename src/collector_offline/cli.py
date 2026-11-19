from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from sqlalchemy import Engine

from collector_offline.application.cleanup import CleanupWorker
from collector_offline.application.collector import LocalCollector
from collector_offline.application.state_machine import StateCoordinator
from collector_offline.application.sync_worker import SyncWorker
from collector_offline.config import AppConfig, load_config, secret_from_env
from collector_offline.infrastructure.database import Database, InstanceLock, build_engine
from collector_offline.infrastructure.drivers import build_bound_reader
from collector_offline.infrastructure.http_client import CentralClient
from collector_offline.infrastructure.repository import EdgeRepository
from collector_offline.migrations import apply_migrations
from collector_offline.observability import configure_logging
from collector_offline.runtime import EdgeRuntime

logger = logging.getLogger("collector_offline.cli")


def _migration_directory() -> Path:
    candidates = (
        Path.cwd() / "migrations" / "local",
        Path(__file__).resolve().parents[2] / "migrations" / "local",
        Path("/opt/crystal/collector-offline/migrations/local"),
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    raise RuntimeError("local migration directory was not found")


def _build(
    config_path: str,
) -> tuple[AppConfig, Engine, EdgeRepository, CentralClient]:
    config, secrets = load_config(config_path)
    sensitive = [secrets.database_url, secrets.central_token]
    for plc in config.plcs:
        for connection in [plc.siemens, plc.rockwell, *plc.opcua_modules]:
            if connection is not None:
                sensitive.extend(
                    filter(
                        None,
                        [
                            secret_from_env(connection.username_env),
                            secret_from_env(connection.password_env),
                        ],
                    )
                )
    configure_logging(
        config.logging.level, config.logging.json_output, config.node.source_node_id, sensitive
    )
    engine = build_engine(secrets.database_url, config.database)
    database = Database(engine)
    repository = EdgeRepository(
        database,
        source_node_id=config.node.source_node_id,
        lagoon_id=config.node.lagoon_id,
    )
    client = CentralClient(
        config.central,
        secrets.central_token,
        config.node.source_node_id,
        config.node.lagoon_id,
        config.node.product_type,
    )
    return config, engine, repository, client


def run_command(config_path: str) -> int:
    config, engine, repository, client = _build(config_path)
    applied = apply_migrations(engine, _migration_directory())
    if applied:
        logger.info("Database migrations applied", extra={"migrations": applied})
    with InstanceLock(engine):
        readers = [build_bound_reader(plc) for plc in config.plcs]
        collector = LocalCollector(repository, readers, config.node.lagoon_id)
        runtime = EdgeRuntime(
            config,
            repository,
            StateCoordinator(repository, config.supervisor),
            collector,
            SyncWorker(repository, client, config.sync),
            CleanupWorker(repository, config.retention),
            client,
        )
        runtime.run()
    return 0


def migrate_command(config_path: str) -> int:
    config, engine, _repository, client = _build(config_path)
    try:
        applied = apply_migrations(engine, _migration_directory())
        print(json.dumps({"applied": applied}))
        return 0
    finally:
        client.close()
        engine.dispose()


def status_command(config_path: str) -> int:
    _config, engine, repository, client = _build(config_path)
    try:
        print(json.dumps(repository.operational_status(), indent=2, default=str))
        return 0
    finally:
        client.close()
        engine.dispose()


def retry_failed_command(config_path: str) -> int:
    _config, engine, repository, client = _build(config_path)
    try:
        requeued = repository.retry_failed()
        print(json.dumps({"requeued": requeued}))
        return 0
    finally:
        client.close()
        engine.dispose()


def main() -> int:
    parser = argparse.ArgumentParser(prog="collector-offline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "migrate", "status", "retry-failed"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--config", required=True)
    args = parser.parse_args()
    if args.command == "run":
        return run_command(args.config)
    if args.command == "migrate":
        return migrate_command(args.config)
    if args.command == "retry-failed":
        return retry_failed_command(args.config)
    return status_command(args.config)
