from __future__ import annotations

from datetime import timedelta

from conftest import SequenceReader, enter_offline, health

from collector_offline.application.collector import LocalCollector
from collector_offline.application.sync_worker import SyncWorker
from collector_offline.config import SupervisorConfig, SyncConfig, TagSpec
from collector_offline.domain.models import AckStatus, BackfillAck, BackfillResponse, NodeState
from collector_offline.infrastructure.drivers import BoundReader


class IdempotentCentral:
    def __init__(self) -> None:
        self.ids = set()
        self.timestamps = {}

    def send_backfill(self, records):
        acks = []
        for record in records:
            status = AckStatus.DUPLICATE if record.local_id in self.ids else AckStatus.ACCEPTED
            self.ids.add(record.local_id)
            self.timestamps[record.local_id] = record.payload.get("bucket") or record.payload.get(
                "started_at"
            )
            acks.append(BackfillAck(record.kind, record.local_id, status))
        return BackfillResponse(acks)


def test_outage_capture_recovery_and_idempotent_backfill(repository, base_time) -> None:
    supervisor = SupervisorConfig(failure_threshold=3, recovery_threshold=1)
    enter_offline(repository, base_time, supervisor)
    assert repository.snapshot().state == NodeState.OFFLINE
    reader = SequenceReader([{"TEMP": 20.0, "P001_ST": 0}, {"TEMP": 21.0, "P001_ST": 1}])
    collector = LocalCollector(
        repository,
        [
            BoundReader(
                "small-sim",
                reader,
                {
                    "TEMP": TagSpec(address="TEMP"),
                    "P001_ST": TagSpec(address="P001_ST", event=True),
                },
            )
        ],
        "small_sim",
    )
    collector.capture(base_time)
    collector.capture(base_time + timedelta(minutes=1))

    repository.process_health(health(True, base_time + timedelta(minutes=2)), supervisor)
    assert repository.snapshot().state == NodeState.SYNCING

    central = IdempotentCentral()
    worker = SyncWorker(repository, central, SyncConfig(batch_size=100))
    first = worker.run_once(base_time + timedelta(minutes=3))
    assert first["accepted"] == 5  # four minute rows plus one state event
    assert worker.run_once(base_time + timedelta(minutes=4))["sync_completed"] == 1
    assert repository.snapshot().state == NodeState.ONLINE
    assert all(value.endswith("+00:00") for value in central.timestamps.values())
