from __future__ import annotations

import hashlib
import json
import logging
import random
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from uuid import UUID

from collector_offline.application.chunk_builder import ChunkBuilder, ChunkOutbox
from collector_offline.config import TransferConfig
from collector_offline.domain.models import HISTORICAL_SOURCE, ChunkStatus, NodeState
from collector_offline.domain.transfer import ChunkDescriptor
from collector_offline.infrastructure.chunk_repository import ChunkRepository
from collector_offline.infrastructure.repository import EdgeRepository
from collector_offline.infrastructure.transfer_client import (
    PartAcknowledgement,
    TransferStatus,
    TransferTransportError,
)

logger = logging.getLogger("collector_offline.transfer")


class TransferTransport(Protocol):
    def create_transfer(self, manifest: dict[str, Any]) -> TransferStatus: ...

    def get_status(self, transfer_id: UUID) -> TransferStatus: ...

    def upload_part(
        self,
        transfer_id: UUID,
        part_number: int,
        *,
        offset: int,
        part_sha256: str,
        data: bytes,
    ) -> PartAcknowledgement: ...

    def complete_transfer(self, transfer_id: UUID) -> TransferStatus: ...

    def close(self) -> None: ...


class ChunkTransferWorker:
    ACTIVE_REMOTE_STATUSES = {"CREATED", "PENDING", "UPLOADING"}
    IMPORT_REMOTE_STATUSES = {
        "RECEIVED",
        "UPLOADED",
        "IMPORTING",
        "VERIFIED",
        "FAILED",
        "QUARANTINED",
    }

    def __init__(
        self,
        edge_repository: EdgeRepository,
        chunk_repository: ChunkRepository,
        outbox: ChunkOutbox,
        transport: TransferTransport,
        config: TransferConfig,
        *,
        jitter: Callable[[float, float], float] = random.uniform,
    ) -> None:
        self.edge_repository = edge_repository
        self.chunk_repository = chunk_repository
        self.outbox = outbox
        self.transport = transport
        self.config = config
        self._jitter = jitter

    def recover(self, now: datetime) -> int:
        return self.chunk_repository.recover_upload_leases(now)

    def run_once(self, now: datetime) -> Counter[str]:
        if self.edge_repository.snapshot().state != NodeState.SYNCING:
            return Counter()
        descriptor = self.chunk_repository.claim_for_transfer(now, self.config)
        if descriptor is None:
            completed = self.edge_repository.complete_sync_if_drained(now)
            return Counter({"sync_completed": 1}) if completed else Counter()

        try:
            descriptor = self._prepare_descriptor(descriptor)
            counts = self._transfer(descriptor, now)
        except TransferTransportError as exc:
            if exc.retryable:
                delay = self._retry_delay(descriptor.retry_count)
                self.chunk_repository.schedule_retry(
                    descriptor,
                    now + timedelta(seconds=delay),
                    f"{exc.category}: {exc}",
                )
                logger.warning(
                    "Chunk transfer scheduled for retry",
                    extra={
                        "chunk_id": str(descriptor.chunk_id),
                        "transfer_id": str(descriptor.transfer_id),
                        "retry_count": descriptor.retry_count,
                        "retry_seconds": round(delay, 3),
                        "error_category": exc.category,
                    },
                )
                return Counter({"retry": 1})
            self._fail_descriptor(
                descriptor,
                f"{exc.category}: {exc}",
                quarantine=exc.quarantine,
            )
            return Counter({"quarantined" if exc.quarantine else "failed": 1})
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            self._fail_descriptor(
                descriptor,
                f"local_chunk_invalid: {type(exc).__name__}: {exc}",
                quarantine=True,
            )
            logger.exception(
                "Local chunk validation failed",
                extra={"chunk_id": str(descriptor.chunk_id)},
            )
            return Counter({"quarantined": 1})

        if counts["verified"]:
            self.edge_repository.complete_sync_if_drained(now)
        return counts

    def _transfer(self, descriptor: ChunkDescriptor, now: datetime) -> Counter[str]:
        if descriptor.status in {ChunkStatus.UPLOADED, ChunkStatus.IMPORTING}:
            remote = self.transport.get_status(descriptor.transfer_id)
            if remote.status.upper() in {"READY", "FAILED"}:
                remote = self._complete_remote(descriptor.transfer_id)
            return self._apply_remote(descriptor, remote, now)

        manifest = self._load_and_validate_manifest(descriptor)
        remote = self.transport.create_transfer(manifest)
        self._persist_confirmed(descriptor, remote.confirmed_parts)
        if remote.status.upper() == "READY":
            completed = self._complete_remote(descriptor.transfer_id)
            return self._apply_remote(descriptor, completed, now)
        if remote.status in self.IMPORT_REMOTE_STATUSES:
            return self._apply_remote(descriptor, remote, now)
        if remote.status not in self.ACTIVE_REMOTE_STATUSES:
            raise TransferTransportError(
                f"central returned unsupported transfer status {remote.status!r}",
                retryable=True,
                category="invalid_response",
            )

        confirmed = set(descriptor.confirmed_parts).union(remote.confirmed_parts)
        missing = set(remote.missing_parts) or (
            set(range(1, descriptor.total_parts + 1)) - confirmed
        )
        if any(part < 1 or part > descriptor.total_parts for part in confirmed | missing):
            raise TransferTransportError(
                "central returned a part outside the manifest range",
                retryable=False,
                category="manifest_mismatch",
                quarantine=True,
            )

        counts: Counter[str] = Counter()
        with descriptor.file_path.open("rb") as stream:
            for part_number in sorted(missing):
                offset = (part_number - 1) * descriptor.part_size_bytes
                expected_size = min(
                    descriptor.part_size_bytes,
                    descriptor.compressed_size_bytes - offset,
                )
                if expected_size <= 0:
                    raise ValueError("part offset is outside the compressed chunk")
                stream.seek(offset)
                data = stream.read(expected_size)
                if len(data) != expected_size:
                    raise ValueError("compressed chunk ended before the expected part boundary")
                part_sha256 = hashlib.sha256(data).hexdigest()
                started = time.monotonic()
                acknowledgement = self.transport.upload_part(
                    descriptor.transfer_id,
                    part_number,
                    offset=offset,
                    part_sha256=part_sha256,
                    data=data,
                )
                elapsed = max(time.monotonic() - started, 0.001)
                self._persist_confirmed(
                    descriptor,
                    (*acknowledgement.confirmed_parts, part_number),
                )
                counts["parts_sent"] += 1
                counts["bytes_sent"] += len(data)
                if acknowledgement.status == "already_received":
                    counts["parts_resumed"] += 1
                logger.info(
                    "Chunk part confirmed",
                    extra={
                        "chunk_id": str(descriptor.chunk_id),
                        "transfer_id": str(descriptor.transfer_id),
                        "part_number": part_number,
                        "part_count": descriptor.total_parts,
                        "part_status": acknowledgement.status,
                        "bytes_sent": len(data),
                        "bytes_per_second": round(len(data) / elapsed, 1),
                    },
                )

        completed = self._complete_remote(descriptor.transfer_id)
        counts.update(self._apply_remote(descriptor, completed, now))
        return counts

    def _complete_remote(self, transfer_id: UUID) -> TransferStatus:
        completed = self.transport.complete_transfer(transfer_id)
        if completed.status not in self.IMPORT_REMOTE_STATUSES:
            raise TransferTransportError(
                f"central did not accept the completed transfer: {completed.status!r}",
                retryable=True,
                category="invalid_response",
            )
        return completed

    def _prepare_descriptor(self, descriptor: ChunkDescriptor) -> ChunkDescriptor:
        file_path, manifest_path = self.outbox.reconcile_paths(descriptor)
        if descriptor.status == ChunkStatus.UPLOADING:
            located = replace(
                descriptor,
                file_path=file_path,
                manifest_path=manifest_path,
            )
            file_path, manifest_path = self.outbox.move_uploading(located)
        if file_path != descriptor.file_path or manifest_path != descriptor.manifest_path:
            self.chunk_repository.update_paths(
                descriptor.chunk_id,
                file_path,
                manifest_path,
            )
            return replace(
                descriptor,
                file_path=file_path,
                manifest_path=manifest_path,
            )
        return descriptor

    def _load_and_validate_manifest(self, descriptor: ChunkDescriptor) -> dict[str, Any]:
        with descriptor.manifest_path.open("r", encoding="utf-8") as stream:
            manifest = json.load(stream)
        if not isinstance(manifest, dict):
            raise ValueError("chunk manifest is not an object")
        expected = {
            "schema_version": 1,
            "transfer_id": str(descriptor.transfer_id),
            "chunk_id": str(descriptor.chunk_id),
            "source": HISTORICAL_SOURCE,
            "source_node_id": descriptor.source_node_id,
            "lagoon_id": descriptor.lagoon_id,
            "data_kind": descriptor.data_kind.value,
            "format": "csv",
            "compression": "gzip",
            "row_count": descriptor.row_count,
            "compressed_size_bytes": descriptor.compressed_size_bytes,
            "part_size_bytes": descriptor.part_size_bytes,
            "total_parts": descriptor.total_parts,
            "sha256": descriptor.sha256,
        }
        for key, value in expected.items():
            if manifest.get(key) != value:
                raise ValueError(f"chunk manifest field {key!r} does not match durable metadata")
        if descriptor.file_path.stat().st_size != descriptor.compressed_size_bytes:
            raise ValueError("compressed chunk size does not match the manifest")
        digest = hashlib.sha256()
        with descriptor.file_path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        if digest.hexdigest() != descriptor.sha256:
            raise ValueError("compressed chunk SHA-256 does not match the manifest")
        return manifest

    def _persist_confirmed(
        self,
        descriptor: ChunkDescriptor,
        confirmed_parts: tuple[int, ...],
    ) -> None:
        for part_number in sorted(set(confirmed_parts)):
            self.chunk_repository.confirm_part(descriptor, part_number)

    def _apply_remote(
        self,
        descriptor: ChunkDescriptor,
        remote: TransferStatus,
        now: datetime,
    ) -> Counter[str]:
        self._persist_confirmed(descriptor, remote.confirmed_parts)
        status = remote.status.upper()
        if status not in self.IMPORT_REMOTE_STATUSES:
            raise TransferTransportError(
                f"central returned unsupported import status {status!r}",
                retryable=True,
                category="invalid_response",
            )
        if status == "FAILED":
            delay = self._retry_delay(descriptor.retry_count)
            self.chunk_repository.schedule_import_retry(
                descriptor,
                now + timedelta(seconds=delay),
                f"central_import_failed: {remote.last_error or 'central retry pending'}",
                now,
            )
            logger.warning(
                "Central import failed and remains scheduled for polling",
                extra={
                    "chunk_id": str(descriptor.chunk_id),
                    "transfer_id": str(descriptor.transfer_id),
                    "retry_count": descriptor.retry_count,
                    "retry_seconds": round(delay, 3),
                },
            )
            return Counter({"retry": 1})

        owned = descriptor
        if status == "VERIFIED":
            file_path, manifest_path = self.outbox.move_verified(descriptor)
            self.chunk_repository.update_paths(descriptor.chunk_id, file_path, manifest_path)
            owned = replace(descriptor, file_path=file_path, manifest_path=manifest_path)
        elif status == "QUARANTINED":
            file_path, manifest_path = self.outbox.move_quarantined(descriptor)
            self.chunk_repository.update_paths(descriptor.chunk_id, file_path, manifest_path)
            owned = replace(descriptor, file_path=file_path, manifest_path=manifest_path)
        local_status = self.chunk_repository.apply_remote_status(
            owned,
            status,
            now,
            self.config,
            confirmed_parts=list(remote.confirmed_parts),
            staged_rows=remote.staged_rows,
            inserted_rows=remote.inserted_rows,
            duplicate_rows=remote.duplicate_rows,
            last_error=remote.last_error,
        )
        logger.info(
            "Central chunk status applied",
            extra={
                "chunk_id": str(descriptor.chunk_id),
                "transfer_id": str(descriptor.transfer_id),
                "chunk_status": local_status.value,
                "confirmed_parts": len(remote.confirmed_parts),
                "staged_rows": remote.staged_rows,
                "inserted_rows": remote.inserted_rows,
                "duplicate_rows": remote.duplicate_rows,
            },
        )
        return Counter({local_status.value.lower(): 1})

    def _fail_descriptor(
        self,
        descriptor: ChunkDescriptor,
        error: str,
        *,
        quarantine: bool,
    ) -> None:
        owned = descriptor
        try:
            if quarantine:
                file_path, manifest_path = self.outbox.move_quarantined(descriptor)
            else:
                file_path, manifest_path = self.outbox.move_failed(descriptor)
            self.chunk_repository.update_paths(descriptor.chunk_id, file_path, manifest_path)
            owned = replace(descriptor, file_path=file_path, manifest_path=manifest_path)
        except OSError:
            logger.exception(
                "Could not move failed chunk files",
                extra={"chunk_id": str(descriptor.chunk_id)},
            )
        self.chunk_repository.fail_permanently(
            owned,
            error,
            quarantine=quarantine,
        )

    def _retry_delay(self, retry_count: int) -> float:
        base = min(
            self.config.retry_initial_seconds * (2 ** max(0, retry_count - 1)),
            self.config.retry_max_seconds,
        )
        return float(base * self._jitter(0.8, 1.2))


class ChunkTransferService:
    def __init__(
        self,
        edge_repository: EdgeRepository,
        chunk_builder: ChunkBuilder,
        transfer_worker: ChunkTransferWorker,
        *,
        interval_seconds: float,
    ) -> None:
        self.edge_repository = edge_repository
        self.chunk_builder = chunk_builder
        self.transfer_worker = transfer_worker
        self.interval_seconds = interval_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._transport_closed = False
        self._next_metrics_at = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        now = datetime.now(UTC)
        recovered_builds = self.chunk_builder.recover(now)
        recovered_uploads = self.transfer_worker.recover(now)
        if recovered_builds or recovered_uploads:
            logger.warning(
                "Recovered interrupted chunk work",
                extra={
                    "creating_chunks": recovered_builds,
                    "upload_leases": recovered_uploads,
                },
            )
        self._stop_event.clear()
        self._transport_closed = False
        self._thread = threading.Thread(
            target=self._run,
            name="collector-offline-transfer",
            daemon=True,
        )
        self._thread.start()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            try:
                now = datetime.now(UTC)
                self.chunk_builder.recover(now)
                self.transfer_worker.recover(now)
                state = self.edge_repository.snapshot().state
                if state == NodeState.OFFLINE:
                    self.chunk_builder.run_once(now)
                elif state == NodeState.SYNCING:
                    self.chunk_builder.run_once(now, force=True)
                    self.transfer_worker.run_once(now)
                monotonic_now = time.monotonic()
                if monotonic_now >= self._next_metrics_at:
                    backlog = self.edge_repository.backlog_counts()
                    oldest = (
                        self.transfer_worker.chunk_repository.oldest_pending_timestamp()
                    )
                    logger.info(
                        "Offline transfer backlog status",
                        extra={
                            "pending_rows": sum(
                                backlog.get(status, 0)
                                for status in ("pending", "syncing", "chunked", "failed")
                            ),
                            "oldest_pending_at": oldest.isoformat() if oldest else None,
                            "chunk_counts": (
                                self.transfer_worker.chunk_repository.chunk_counts()
                            ),
                        },
                    )
                    self._next_metrics_at = monotonic_now + 60
            except Exception:
                logger.exception("Unhandled chunk service iteration failure")
            self._stop_event.wait(self.interval_seconds)

    def stop(self, timeout: float | None = None) -> None:
        self._stop_event.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=timeout)
            if thread.is_alive():
                logger.warning("Chunk transfer thread did not stop before timeout")
                return
        self._thread = None
        if not self._transport_closed:
            self.transfer_worker.transport.close()
            self._transport_closed = True
