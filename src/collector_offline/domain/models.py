from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID


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
    SENT = "sent"
    FAILED = "failed"


class DataQuality(StrEnum):
    GOOD = "good"
    BAD = "bad"
    UNCERTAIN = "uncertain"


class AckStatus(StrEnum):
    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    REJECTED = "rejected"


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
    quality: DataQuality = DataQuality.GOOD


@dataclass(frozen=True, slots=True)
class BackfillRecord:
    kind: str
    local_id: UUID
    payload: dict[str, Any]
    attempts: int
    lease_token: UUID


@dataclass(frozen=True, slots=True)
class BackfillAck:
    kind: str
    local_id: UUID
    status: AckStatus
    retryable: bool = False
    code: str | None = None
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class BackfillResponse:
    acknowledgements: list[BackfillAck] = field(default_factory=list)
    correlation_id: str | None = None
