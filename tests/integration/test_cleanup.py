from __future__ import annotations

from datetime import timedelta

from conftest import SequenceReader, enter_offline
from sqlalchemy import func, select

from collector_offline.application.collector import LocalCollector
from collector_offline.config import RetentionConfig, TagSpec
from collector_offline.domain.models import SyncStatus
from collector_offline.infrastructure.drivers import BoundReader
from collector_offline.infrastructure.models import LocalScadaEventRow, LocalScadaMinuteRow


def test_cleanup_only_removes_old_sent_closed_data(repository, database, base_time) -> None:
    enter_offline(repository, base_time)
    reader = SequenceReader(
        [
            {"SENT": 1.0, "PENDING": 2.0, "FAILED": 3.0, "LEASED": 4.0, "P001_ST": 0},
            {"SENT": 1.0, "PENDING": 2.0, "FAILED": 3.0, "LEASED": 4.0, "P001_ST": 1},
        ]
    )
    specs = {
        "SENT": TagSpec(address="SENT"),
        "PENDING": TagSpec(address="PENDING"),
        "FAILED": TagSpec(address="FAILED"),
        "LEASED": TagSpec(address="LEASED"),
        "P001_ST": TagSpec(address="P001_ST", event=True),
    }
    collector = LocalCollector(repository, [BoundReader("sim", reader, specs)], "small_sim")
    collector.capture(base_time)
    collector.capture(base_time + timedelta(minutes=1))

    old = base_time - timedelta(days=100)
    with database.session() as db, db.begin():
        rows = {row.tag_id: row for row in db.scalars(select(LocalScadaMinuteRow)).all()}
        rows["SENT"].sync_status = SyncStatus.SENT.value
        rows["SENT"].synced_at = old
        rows["PENDING"].sync_status = SyncStatus.PENDING.value
        rows["FAILED"].sync_status = SyncStatus.FAILED.value
        rows["LEASED"].sync_status = SyncStatus.SYNCING.value
        rows["LEASED"].lease_until = base_time + timedelta(hours=1)
        rows["LEASED"].lease_token = rows["LEASED"].id
        event = db.scalars(select(LocalScadaEventRow)).one()
        event.sync_status = SyncStatus.SENT.value
        event.synced_at = old

    deleted = repository.cleanup(base_time, RetentionConfig(days=90, batch_size=100))

    assert deleted == {"minutes": 1, "events": 0, "sessions": 0}
    with database.session() as db:
        remaining_tags = set(db.scalars(select(LocalScadaMinuteRow.tag_id)).all())
        assert {"PENDING", "FAILED", "LEASED"}.issubset(remaining_tags)
        assert db.scalar(select(func.count()).select_from(LocalScadaEventRow)) == 1
