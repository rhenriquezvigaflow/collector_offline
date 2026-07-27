from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from collector_offline.config import SupervisorConfig
from collector_offline.domain.models import HealthResult
from collector_offline.infrastructure.database import Database
from collector_offline.infrastructure.models import Base
from collector_offline.infrastructure.repository import EdgeRepository


@pytest.fixture
def database() -> Database:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        future=True,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    database = Database(engine)
    yield database
    engine.dispose()


@pytest.fixture
def repository(database: Database) -> EdgeRepository:
    repository = EdgeRepository(database, "edge-test-01", "small_sim")
    repository.initialize_runtime()
    return repository


@pytest.fixture
def base_time() -> datetime:
    return datetime(2026, 7, 22, 12, 0, tzinfo=UTC)


def health(available: bool, at: datetime, reason: str | None = None) -> HealthResult:
    return HealthResult(available=available, checked_at=at, reason=reason)


def enter_offline(
    repository: EdgeRepository,
    at: datetime,
    config: SupervisorConfig | None = None,
) -> None:
    thresholds = config or SupervisorConfig(failure_threshold=3, recovery_threshold=2)
    for offset in range(thresholds.failure_threshold):
        repository.process_health(
            health(False, at + timedelta(seconds=offset), "central unavailable"), thresholds
        )


class SequenceReader:
    def __init__(self, values: list[dict[str, Any]]) -> None:
        self.values = list(values)
        self.calls = 0
        self.closed = False

    def read_once(self) -> dict[str, Any]:
        index = min(self.calls, len(self.values) - 1)
        self.calls += 1
        return dict(self.values[index])

    def close(self) -> None:
        self.closed = True
