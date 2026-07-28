from __future__ import annotations

import csv
import gzip
import hashlib
import json
import logging
import math
import os
import shutil
import time
from datetime import datetime
from pathlib import Path
from uuid import UUID

from collector_offline.config import ChunkConfig, TransferConfig
from collector_offline.domain.models import HISTORICAL_SOURCE, DataKind
from collector_offline.domain.transfer import (
    ChunkBuildClaim,
    ChunkDescriptor,
    SealedChunk,
    csv_header,
    utc_text,
)
from collector_offline.infrastructure.chunk_repository import ChunkRepository

logger = logging.getLogger("collector_offline.chunk_builder")


class ChunkOutbox:
    DIRECTORY_NAMES = (
        "creating",
        "pending",
        "uploading",
        "verified",
        "failed",
        "quarantine",
    )

    def __init__(self, root: Path) -> None:
        self.root = root

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o750)
        for name in self.DIRECTORY_NAMES:
            (self.root / name).mkdir(exist_ok=True, mode=0o750)

    @staticmethod
    def _sync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _base_name(claim: ChunkBuildClaim) -> str:
        return (
            f"chunk-{claim.sequence_number:010d}-{claim.data_kind.value}-"
            f"{claim.chunk_id}"
        )

    def seal(self, claim: ChunkBuildClaim) -> SealedChunk:
        self.ensure()
        base = self._base_name(claim)
        creating = self.root / "creating"
        pending = self.root / "pending"
        csv_part = creating / f"{base}.csv.part"
        gzip_part = creating / f"{base}.csv.gz.part"
        manifest_part = creating / f"{base}.manifest.json.part"
        final_file = pending / f"{base}.csv.gz"
        final_manifest = pending / f"{base}.manifest.json"

        with csv_part.open("w", encoding="utf-8", newline="") as stream:
            writer = csv.writer(stream, lineterminator="\n")
            writer.writerow(csv_header(claim.data_kind))
            for record in claim.records:
                writer.writerow(record.values)
            stream.flush()
            os.fsync(stream.fileno())
        uncompressed_size = csv_part.stat().st_size

        with csv_part.open("rb") as source, gzip_part.open("wb") as destination:
            with gzip.GzipFile(
                filename="", mode="wb", fileobj=destination, mtime=0
            ) as compressed:
                shutil.copyfileobj(source, compressed, length=1024 * 1024)
            destination.flush()
            os.fsync(destination.fileno())

        compressed_size = gzip_part.stat().st_size
        sha256 = self._sha256(gzip_part)
        total_parts = max(1, math.ceil(compressed_size / claim.part_size_bytes))
        self._validate_gzip(gzip_part, claim)

        manifest = {
            "schema_version": 1,
            "transfer_id": str(claim.transfer_id),
            "chunk_id": str(claim.chunk_id),
            "sequence": claim.sequence_number,
            "source": HISTORICAL_SOURCE,
            "source_node_id": claim.source_node_id,
            "lagoon_id": claim.lagoon_id,
            "data_kind": claim.data_kind.value,
            "format": "csv",
            "compression": "gzip",
            "file_name": final_file.name,
            "row_count": len(claim.records),
            "uncompressed_size_bytes": uncompressed_size,
            "compressed_size_bytes": compressed_size,
            "part_size_bytes": claim.part_size_bytes,
            "total_parts": total_parts,
            "from_timestamp": utc_text(claim.from_timestamp),
            "to_timestamp": utc_text(claim.to_timestamp),
            "sha256": sha256,
        }
        with manifest_part.open("w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                manifest,
                stream,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())

        os.replace(gzip_part, final_file)
        os.replace(manifest_part, final_manifest)
        self._sync_directory(pending)
        csv_part.unlink(missing_ok=True)
        return SealedChunk(
            file_path=final_file,
            manifest_path=final_manifest,
            uncompressed_size_bytes=uncompressed_size,
            compressed_size_bytes=compressed_size,
            sha256=sha256,
            total_parts=total_parts,
        )

    @staticmethod
    def _validate_gzip(path: Path, claim: ChunkBuildClaim) -> None:
        with gzip.open(path, "rt", encoding="utf-8", newline="") as stream:
            reader = csv.reader(stream)
            if tuple(next(reader)) != csv_header(claim.data_kind):
                raise ValueError("generated CSV header is invalid")
            count = 0
            for row in reader:
                if not row or row[-1] != HISTORICAL_SOURCE:
                    raise ValueError("generated CSV contains an invalid source")
                count += 1
        if count != len(claim.records):
            raise ValueError("generated CSV row count changed during validation")

    def quarantine_chunk(self, chunk_id: UUID) -> list[Path]:
        self.ensure()
        moved: list[Path] = []
        marker = str(chunk_id)
        quarantine = self.root / "quarantine"
        for directory_name in self.DIRECTORY_NAMES:
            if directory_name == "quarantine":
                continue
            directory = self.root / directory_name
            for path in directory.glob(f"*{marker}*"):
                destination = quarantine / path.name
                os.replace(path, destination)
                moved.append(destination)
        if moved:
            self._sync_directory(quarantine)
        return moved

    def move_verified(self, descriptor: ChunkDescriptor) -> tuple[Path, Path]:
        return self._move_sealed(descriptor, "verified")

    def move_failed(self, descriptor: ChunkDescriptor) -> tuple[Path, Path]:
        return self._move_sealed(descriptor, "failed")

    def move_uploading(self, descriptor: ChunkDescriptor) -> tuple[Path, Path]:
        return self._move_sealed(descriptor, "uploading")

    def move_quarantined(self, descriptor: ChunkDescriptor) -> tuple[Path, Path]:
        return self._move_sealed(descriptor, "quarantine")

    def reconcile_paths(self, descriptor: ChunkDescriptor) -> tuple[Path, Path]:
        """Locate files after a crash between an atomic rename and the DB commit."""
        self.ensure()
        return (
            self._locate_file(descriptor.file_path),
            self._locate_file(descriptor.manifest_path),
        )

    def _locate_file(self, recorded_path: Path) -> Path:
        if recorded_path.exists():
            return recorded_path
        candidates = [
            self.root / directory_name / recorded_path.name
            for directory_name in self.DIRECTORY_NAMES
            if (self.root / directory_name / recorded_path.name).exists()
        ]
        if len(candidates) != 1:
            raise FileNotFoundError(
                f"expected exactly one outbox file named {recorded_path.name!r}; "
                f"found {len(candidates)}"
            )
        return candidates[0]

    def _move_sealed(
        self,
        descriptor: ChunkDescriptor,
        directory_name: str,
    ) -> tuple[Path, Path]:
        self.ensure()
        destination_directory = self.root / directory_name
        file_path = destination_directory / descriptor.file_path.name
        manifest_path = destination_directory / descriptor.manifest_path.name
        if descriptor.file_path != file_path and descriptor.file_path.exists():
            os.replace(descriptor.file_path, file_path)
        if descriptor.manifest_path != manifest_path and descriptor.manifest_path.exists():
            os.replace(descriptor.manifest_path, manifest_path)
        if not file_path.exists() or not manifest_path.exists():
            raise FileNotFoundError(f"{directory_name} chunk files are missing")
        self._sync_directory(destination_directory)
        return file_path, manifest_path

    def delete_verified_files(self, descriptor: ChunkDescriptor) -> None:
        verified = (self.root / "verified").resolve()
        for candidate in (descriptor.file_path, descriptor.manifest_path):
            resolved = candidate.resolve()
            if resolved.parent != verified:
                raise ValueError("refusing to delete a file outside verified outbox")
            resolved.unlink(missing_ok=True)
        self._sync_directory(verified)


class ChunkBuilder:
    def __init__(
        self,
        repository: ChunkRepository,
        outbox: ChunkOutbox,
        chunk_config: ChunkConfig,
        transfer_config: TransferConfig,
    ) -> None:
        self.repository = repository
        self.outbox = outbox
        self.chunk_config = chunk_config
        self.transfer_config = transfer_config

    def recover(self, now: datetime) -> int:
        self.outbox.ensure()
        recovered = 0
        for chunk_id in self.repository.expired_creating_ids(now):
            self.outbox.quarantine_chunk(chunk_id)
            recovered += int(
                self.repository.abort_creation(
                    chunk_id, "recovered abandoned chunk creation"
                )
            )
        return recovered

    def run_once(self, now: datetime, *, force: bool = False) -> list[UUID]:
        built: list[UUID] = []
        for data_kind in (DataKind.MINUTE, DataKind.EVENT):
            claim = self.repository.claim_for_build(
                data_kind,
                now,
                self.chunk_config,
                self.transfer_config.part_size_bytes,
                force=force,
            )
            if claim is None:
                continue
            started = time.monotonic()
            try:
                sealed = self.outbox.seal(claim)
                self.repository.seal(claim, sealed, now)
            except Exception as exc:
                self.outbox.quarantine_chunk(claim.chunk_id)
                self.repository.abort_creation(claim.chunk_id, str(exc))
                logger.exception(
                    "Chunk creation failed",
                    extra={"chunk_id": str(claim.chunk_id), "data_kind": data_kind.value},
                )
                continue
            elapsed = max(time.monotonic() - started, 0.001)
            ratio = sealed.uncompressed_size_bytes / max(sealed.compressed_size_bytes, 1)
            logger.info(
                "Chunk sealed",
                extra={
                    "chunk_id": str(claim.chunk_id),
                    "data_kind": data_kind.value,
                    "row_count": len(claim.records),
                    "uncompressed_size_bytes": sealed.uncompressed_size_bytes,
                    "compressed_size_bytes": sealed.compressed_size_bytes,
                    "compression_ratio": round(ratio, 3),
                    "total_parts": sealed.total_parts,
                    "build_seconds": round(elapsed, 3),
                },
            )
            built.append(claim.chunk_id)
        return built
