from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from sqlalchemy import Engine

from collector_offline.application.chunk_builder import ChunkBuilder, ChunkOutbox
from collector_offline.application.cleanup import CleanupWorker
from collector_offline.application.collector import LocalCollector
from collector_offline.application.state_machine import StateCoordinator
from collector_offline.application.transfer_worker import (
    ChunkTransferService,
    ChunkTransferWorker,
)
from collector_offline.config import AppConfig, load_app_config, load_config
from collector_offline.infrastructure.chunk_repository import ChunkRepository
from collector_offline.infrastructure.database import Database, InstanceLock, build_engine
from collector_offline.infrastructure.drivers import build_bound_reader
from collector_offline.infrastructure.http_client import CentralClient
from collector_offline.infrastructure.repository import EdgeRepository
from collector_offline.infrastructure.transfer_client import TransferClient
from collector_offline.migrations import apply_migrations
from collector_offline.observability import configure_logging
from collector_offline.runtime import EdgeRuntime

logger = logging.getLogger("collector_offline.cli")


def _combined_status(
    repository: EdgeRepository,
    chunks: ChunkRepository,
) -> dict[str, object]:
    status = repository.operational_status()
    status["chunks"] = chunks.chunk_counts()
    status["oldest_pending_at"] = chunks.oldest_pending_timestamp()
    return status


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
) -> tuple[AppConfig, Engine, EdgeRepository, CentralClient, str]:
    config, secrets = load_config(config_path)
    configure_logging(config.logging.level, config.logging.json_output, config.node.source_node_id)
    engine = build_engine(secrets.database_url, config.database)
    database = Database(engine)
    repository = EdgeRepository(
        database,
        source_node_id=config.node.source_node_id,
        lagoon_id=config.node.lagoon_id,
    )
    client = CentralClient(config.central)
    return config, engine, repository, client, secrets.central_token


def run_command(config_path: str) -> int:
    config, engine, repository, client, token = _build(config_path)
    applied = apply_migrations(engine, _migration_directory())
    if applied:
        logger.info("Database migrations applied", extra={"migrations": applied})
    with InstanceLock(engine):
        readers = [build_bound_reader(plc) for plc in config.plcs]
        collector = LocalCollector(repository, readers, config.node.lagoon_id)
        chunk_repository = ChunkRepository(
            repository.database,
            config.node.source_node_id,
            config.node.lagoon_id,
        )
        outbox = ChunkOutbox(config.chunk.outbox_path)
        transfer_client = TransferClient(config.central, config.transfer, token)
        transfer_worker = ChunkTransferWorker(
            repository,
            chunk_repository,
            outbox,
            transfer_client,
            config.transfer,
        )
        runtime = EdgeRuntime(
            config,
            repository,
            StateCoordinator(repository, config.supervisor),
            collector,
            ChunkTransferService(
                repository,
                ChunkBuilder(
                    chunk_repository,
                    outbox,
                    config.chunk,
                    config.transfer,
                ),
                transfer_worker,
                interval_seconds=config.transfer.poll_interval_seconds,
            ),
            CleanupWorker(
                repository,
                config.retention,
                chunk_repository,
                outbox,
            ),
            client,
            status_provider=lambda: _combined_status(repository, chunk_repository),
        )
        runtime.run()
    return 0


def migrate_command(config_path: str) -> int:
    config, engine, _repository, client, _token = _build(config_path)
    try:
        applied = apply_migrations(engine, _migration_directory())
        print(json.dumps({"applied": applied}))
        return 0
    finally:
        client.close()
        engine.dispose()


def status_command(config_path: str) -> int:
    _config, engine, repository, client, _token = _build(config_path)
    try:
        chunks = ChunkRepository(
            repository.database,
            repository.source_node_id,
            repository.lagoon_id,
        )
        status = _combined_status(repository, chunks)
        print(json.dumps(status, indent=2, default=str))
        return 0
    finally:
        client.close()
        engine.dispose()


def retry_failed_command(config_path: str) -> int:
    _config, engine, repository, client, _token = _build(config_path)
    try:
        row_count = repository.retry_failed()
        chunks = ChunkRepository(
            repository.database,
            repository.source_node_id,
            repository.lagoon_id,
        )
        chunk_count = chunks.retry_failed()
        print(json.dumps({"rows_requeued": row_count, "chunks_requeued": chunk_count}))
        return 0
    finally:
        client.close()
        engine.dispose()


def validate_command(config_path: str) -> int:
    config = load_app_config(config_path)
    print(
        json.dumps(
            {
                "valid": True,
                "source_node_id": config.node.source_node_id,
                "lagoon_id": config.node.lagoon_id,
                "product_type": config.node.product_type,
                "plcs": [
                    {
                        "name": plc.name,
                        "source": plc.source,
                        "tag_count": len(plc.tags)
                        + sum(len(module.tags) for module in plc.opcua_modules),
                    }
                    for plc in config.plcs
                ],
                "central_health_url": config.central.health_url,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="collector-offline")
    subparsers = parser.add_subparsers(dest="command", required=True)
    for name in ("run", "migrate", "status", "retry-failed", "validate"):
        subparser = subparsers.add_parser(name)
        subparser.add_argument("--config", required=True)
    args = parser.parse_args()
    if args.command == "validate":
        return validate_command(args.config)
    if args.command == "run":
        return run_command(args.config)
    if args.command == "migrate":
        return migrate_command(args.config)
    if args.command == "retry-failed":
        return retry_failed_command(args.config)
    return status_command(args.config)
