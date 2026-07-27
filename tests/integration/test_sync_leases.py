from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from conftest import SequenceReader, enter_offline, health
from sqlalchemy import select

from collector_offline.application.collector import LocalCollector
from collector_offline.application.sync_worker import SyncWorker
from collector_offline.config import SupervisorConfig, SyncConfig, TagSpec
from collector_offline.domain.models import (
    AckStatus,
    BackfillAck,
    BackfillResponse,
    NodeState,
)
from collector_offline.infrastructure.drivers import BoundReader
from collector_offline.infrastructure.models import LocalScadaMinuteRow
from collector_offline.infrastructure.repository import EdgeRepository


class ScriptedTransport:
    def __init__(self, responder) -> None:
        self.responder = responder
        self.batches = []

    def send_backfill(self, records):
        self.batches.append(records)
        return self.responder(records)


def _capture_two(repository, base_time):
    reader = SequenceReader([{"TEMP": 20.0}, {"TEMP": 21.0}])
    collector = LocalCollector(
        repository,
        [BoundReader("sim", reader, {"TEMP": TagSpec(address="TEMP")})],
        "small_sim",
    )
    collector.capture(base_time)
    collector.capture(base_time + timedelta(minutes=1))


def _enter_syncing(repository, base_time):
    config = SupervisorConfig(failure_threshold=3, recovery_threshold=1)
    repository.process_health(health(True, base_time + timedelta(minutes=2)), config)
    assert repository.snapshot().state == NodeState.SYNCING


def test_partial_ack_retries_without_marking_missing_record_sent(
    repository, database, base_time
) -> None:
    enter_offline(repository, base_time)
    _capture_two(repository, base_time)
    _enter_syncing(repository, base_time)
    sync_config = SyncConfig(batch_size=10, backoff_base_sec=1, backoff_max_sec=1)

    def partial(records):
        first = records[0]
        return BackfillResponse([BackfillAck(first.kind, first.local_id, AckStatus.ACCEPTED)])

    first_transport = ScriptedTransport(partial)
    first_counts = SyncWorker(repository, first_transport, sync_config).run_once(
        base_time + timedelta(minutes=3)
    )

    assert first_counts["accepted"] == 1
    assert first_counts["pending"] == 1
    with database.session() as db:
        statuses = sorted(db.scalars(select(LocalScadaMinuteRow.sync_status)).all())
        assert statuses == ["pending", "sent"]

    def accept_all(records):
        return BackfillResponse(
            [BackfillAck(record.kind, record.local_id, AckStatus.DUPLICATE) for record in records]
        )

    worker = SyncWorker(repository, ScriptedTransport(accept_all), sync_config)
    second_counts = worker.run_once(base_time + timedelta(minutes=4))
    assert second_counts["duplicate"] == 1
    assert worker.run_once(base_time + timedelta(minutes=5))["sync_completed"] == 1
    assert repository.snapshot().state == NodeState.ONLINE


def test_expired_lease_is_recovered_after_syncing_restart(repository, database, base_time) -> None:
    enter_offline(repository, base_time)
    _capture_two(repository, base_time)
    _enter_syncing(repository, base_time)
    sync_config = SyncConfig(batch_size=1, lease_sec=10)
    claim_time = datetime.now(UTC) - timedelta(minutes=1)
    first_claim = repository.claim_backlog(claim_time, sync_config)
    assert len(first_claim) == 1

    restarted = EdgeRepository(database, "edge-test-01", "small_sim")
    restarted.initialize_runtime()
    assert restarted.snapshot().state == NodeState.SYNCING
    second_claim = restarted.claim_backlog(datetime.now(UTC), sync_config)

    assert len(second_claim) == 1
    assert second_claim[0].local_id == first_claim[0].local_id
    assert second_claim[0].attempts == 2


def test_unexpired_lease_prevents_second_instance_claim(repository, database, base_time) -> None:
    enter_offline(repository, base_time)
    _capture_two(repository, base_time)
    _enter_syncing(repository, base_time)
    sync_config = SyncConfig(batch_size=10, lease_sec=120)
    first_claim = repository.claim_backlog(base_time + timedelta(minutes=3), sync_config)

    second = EdgeRepository(database, "edge-test-01", "small_sim")
    second_claim = second.claim_backlog(base_time + timedelta(minutes=3, seconds=1), sync_config)

    assert len(first_claim) == 2
    assert second_claim == []


def test_permanent_and_retryable_rejections_are_distinguished(
    repository, database, base_time
) -> None:
    enter_offline(repository, base_time)
    _capture_two(repository, base_time)
    _enter_syncing(repository, base_time)
    config = SyncConfig(batch_size=10, max_attempts=3, backoff_base_sec=1, backoff_max_sec=1)
    records = repository.claim_backlog(base_time + timedelta(minutes=3), config, uuid4())
    response = BackfillResponse(
        [
            BackfillAck(
                records[0].kind,
                records[0].local_id,
                AckStatus.REJECTED,
                retryable=False,
                detail="unknown tag",
            ),
            BackfillAck(
                records[1].kind,
                records[1].local_id,
                AckStatus.REJECTED,
                retryable=True,
                detail="lock timeout",
            ),
        ]
    )

    counts = repository.finalize_claim(records, response, base_time + timedelta(minutes=3), config)

    assert counts == {"failed": 1, "pending": 1}
    with database.session() as db:
        statuses = sorted(db.scalars(select(LocalScadaMinuteRow.sync_status)).all())
        assert statuses == ["failed", "pending"]

    assert repository.retry_failed() == 1
    with database.session() as db:
        statuses = list(db.scalars(select(LocalScadaMinuteRow.sync_status)).all())
        assert statuses.count("pending") == 2
