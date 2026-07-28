from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from uuid import UUID

HISTORICAL_SOURCE = "collector_offline"


class NodeState(StrEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    SYNCING = "syncing"


class SessionStatus(StrEnum):
    OFFLINE = "offline"
    SYNCING = "syncing"
    COMPLETED = "completed"


class SyncStatus(StrEnum):
    PENDING = "pending"
    SYNCING = "syncing"
    CHUNKED = "chunked"
    SENT = "sent"
    FAILED = "failed"


class DataKind(StrEnum):
    MINUTE = "minute"
    EVENT = "event"


class ChunkStatus(StrEnum):
    CREATING = "CREATING"
    SEALED = "SEALED"
    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    UPLOADED = "UPLOADED"
    IMPORTING = "IMPORTING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    QUARANTINED = "QUARANTINED"


CHUNK_TRANSITIONS: dict[ChunkStatus, frozenset[ChunkStatus]] = {
    ChunkStatus.CREATING: frozenset({ChunkStatus.SEALED, ChunkStatus.FAILED}),
    ChunkStatus.SEALED: frozenset({ChunkStatus.PENDING, ChunkStatus.QUARANTINED}),
    ChunkStatus.PENDING: frozenset({ChunkStatus.UPLOADING, ChunkStatus.QUARANTINED}),
    ChunkStatus.UPLOADING: frozenset(
        {
            ChunkStatus.PENDING,
            ChunkStatus.UPLOADED,
            ChunkStatus.IMPORTING,
            ChunkStatus.VERIFIED,
            ChunkStatus.FAILED,
            ChunkStatus.QUARANTINED,
        }
    ),
    ChunkStatus.UPLOADED: frozenset(
        {ChunkStatus.IMPORTING, ChunkStatus.VERIFIED, ChunkStatus.FAILED, ChunkStatus.QUARANTINED}
    ),
    ChunkStatus.IMPORTING: frozenset(
        {ChunkStatus.VERIFIED, ChunkStatus.FAILED, ChunkStatus.QUARANTINED}
    ),
    ChunkStatus.VERIFIED: frozenset(),
    ChunkStatus.FAILED: frozenset({ChunkStatus.PENDING, ChunkStatus.QUARANTINED}),
    ChunkStatus.QUARANTINED: frozenset(),
}


def require_chunk_transition(before: ChunkStatus | str, after: ChunkStatus | str) -> None:
    current = ChunkStatus(before)
    target = ChunkStatus(after)
    if target == current:
        return
    if target not in CHUNK_TRANSITIONS[current]:
        raise ValueError(f"invalid chunk transition: {current.value} -> {target.value}")


@dataclass(frozen=True, slots=True)
class HealthResult:
    available: bool
    checked_at: datetime
    reason: str | None = None
    status_code: int | None = None
    latency_ms: float | None = None


@dataclass(frozen=True, slots=True)
class StateSnapshot:
    state: NodeState
    failure_count: int
    recovery_count: int
    current_session_id: UUID | None
    last_health_at: datetime | None = None
    last_health_ok: bool | None = None
    last_error: str | None = None


@dataclass(frozen=True, slots=True)
class TransitionResult:
    before: NodeState
    after: NodeState
    reason: str
    session_id: UUID | None

    @property
    def changed(self) -> bool:
        return self.before != self.after


@dataclass(frozen=True, slots=True)
class StorageValue:
    state: int | None = None
    value_num: float | None = None
    value_bool: bool | None = None

    def has_value(self) -> bool:
        return sum(v is not None for v in (self.state, self.value_num, self.value_bool)) == 1


@dataclass(frozen=True, slots=True)
class TagObservation:
    lagoon_id: str
    tag_id: str
    bucket: datetime
    captured_at: datetime
    value: StorageValue

