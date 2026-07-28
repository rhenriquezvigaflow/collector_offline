from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from collector_offline.domain.models import (
    HISTORICAL_SOURCE,
    ChunkStatus,
    DataKind,
)

MINUTE_HEADER = (
    "lagoon_id",
    "tag_id",
    "bucket",
    "state",
    "value_num",
    "value_bool",
    "source",
)
EVENT_HEADER = (
    "id",
    "lagoon_id",
    "tag_id",
    "tag_label",
    "alert_type",
    "previous_state",
    "state",
    "start_ts",
    "end_ts",
    "duration_sec",
    "source",
)


def csv_header(data_kind: DataKind | str) -> tuple[str, ...]:
    return MINUTE_HEADER if DataKind(data_kind) == DataKind.MINUTE else EVENT_HEADER


def utc_text(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def csv_scalar(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float):
        return repr(value)
    return str(value)


def encode_csv_row(values: tuple[str, ...]) -> bytes:
    output = io.StringIO(newline="")
    csv.writer(output, lineterminator="\n").writerow(values)
    return output.getvalue().encode("utf-8")


@dataclass(frozen=True, slots=True)
class CsvRecord:
    local_id: UUID
    timestamp: datetime
    values: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.values or self.values[-1] != HISTORICAL_SOURCE:
            raise ValueError("CSV records must use source=collector_offline")


@dataclass(frozen=True, slots=True)
class ChunkBuildClaim:
    chunk_id: UUID
    transfer_id: UUID
    sequence_number: int
    data_kind: DataKind
    source_node_id: str
    lagoon_id: str
    part_size_bytes: int
    records: tuple[CsvRecord, ...]
    lease_token: UUID

    @property
    def from_timestamp(self) -> datetime:
        return self.records[0].timestamp

    @property
    def to_timestamp(self) -> datetime:
        return self.records[-1].timestamp


@dataclass(frozen=True, slots=True)
class ChunkDescriptor:
    chunk_id: UUID
    transfer_id: UUID
    sequence_number: int
    data_kind: DataKind
    source_node_id: str
    lagoon_id: str
    file_path: Path
    manifest_path: Path
    from_timestamp: datetime
    to_timestamp: datetime
    row_count: int
    uncompressed_size_bytes: int
    compressed_size_bytes: int
    sha256: str
    part_size_bytes: int
    total_parts: int
    confirmed_parts: tuple[int, ...]
    status: ChunkStatus
    retry_count: int
    lease_token: UUID | None


@dataclass(frozen=True, slots=True)
class SealedChunk:
    file_path: Path
    manifest_path: Path
    uncompressed_size_bytes: int
    compressed_size_bytes: int
    sha256: str
    total_parts: int
