from __future__ import annotations

from datetime import UTC, timedelta, timezone

from conftest import SequenceReader, enter_offline, health
from sqlalchemy import func, select

from collector_offline.application.collector import LocalCollector
from collector_offline.config import SupervisorConfig, TagSpec
from collector_offline.domain.models import NodeState
from collector_offline.infrastructure.drivers import BoundReader
from collector_offline.infrastructure.models import (
    EdgeTagStateRow,
    LocalScadaEventRow,
    LocalScadaMinuteRow,
)


def _collector(repository, sequence, specs):
    reader = SequenceReader(sequence)
    collector = LocalCollector(
        repository,
        [BoundReader("sim", reader, specs)],
        "small_sim",
    )
    return collector, reader


def test_online_never_reads_plc(repository, base_time) -> None:
    collector, reader = _collector(repository, [{"TEMP": 20.0}], {"TEMP": TagSpec(address="TEMP")})

    assert collector.capture(base_time) == (0, 0)
    assert reader.calls == 0


def test_capture_is_idempotent_per_utc_minute(repository, database, base_time) -> None:
    enter_offline(repository, base_time)
    collector, _ = _collector(
        repository,
        [{"TEMP": 20.0}, {"TEMP": 21.0}],
        {"TEMP": TagSpec(address="TEMP", data_type="float")},
    )

    assert collector.capture(base_time + timedelta(seconds=10))[0] == 1
    assert collector.capture(base_time + timedelta(seconds=40))[0] == 0

    with database.session() as db:
        row = db.scalars(select(LocalScadaMinuteRow)).one()
        assert row.value_num == 20.0
        assert row.bucket.minute == base_time.minute
        assert db.scalar(select(func.count()).select_from(LocalScadaMinuteRow)) == 1


def test_state_change_creates_one_event_and_repeats_do_not(repository, database, base_time) -> None:
    enter_offline(repository, base_time)
    collector, _ = _collector(
        repository,
        [{"P001_ST": 0}, {"P001_ST": 1}, {"P001_ST": 1}],
        {"P001_ST": TagSpec(address="P001_ST", data_type="state", event=True)},
    )

    collector.capture(base_time)
    assert collector.capture(base_time + timedelta(minutes=1))[1] == 1
    assert collector.capture(base_time + timedelta(minutes=2))[1] == 0

    with database.session() as db:
        event = db.scalars(select(LocalScadaEventRow)).one()
        assert event.previous_state == 0
        assert event.state == 1
        assert event.is_open is True
        tag_state = db.get(EdgeTagStateRow, ("small_sim", "P001_ST"))
        assert tag_state.open_event_id == event.id


def test_open_event_is_closed_by_next_change(repository, database, base_time) -> None:
    enter_offline(repository, base_time)
    collector, _ = _collector(
        repository,
        [{"P001_ST": 0}, {"P001_ST": 1}, {"P001_ST": 0}],
        {"P001_ST": TagSpec(address="P001_ST", data_type="state", event=True)},
    )
    collector.capture(base_time)
    collector.capture(base_time + timedelta(minutes=1))
    collector.capture(base_time + timedelta(minutes=2))

    with database.session() as db:
        events = list(
            db.scalars(select(LocalScadaEventRow).order_by(LocalScadaEventRow.started_at))
        )
        assert len(events) == 2
        assert events[0].is_open is False
        assert events[0].ended_at == events[1].started_at
        assert events[1].is_open is True


def test_recovery_closes_events_and_stops_capture_before_sync(
    repository, database, base_time
) -> None:
    config = SupervisorConfig(failure_threshold=3, recovery_threshold=1)
    enter_offline(repository, base_time, config)
    collector, reader = _collector(
        repository,
        [{"P001_ST": 0}, {"P001_ST": 1}, {"P001_ST": 0}],
        {"P001_ST": TagSpec(address="P001_ST", data_type="state", event=True)},
    )
    collector.capture(base_time)
    collector.capture(base_time + timedelta(minutes=1))
    calls_before_recovery = reader.calls
    restored_at = base_time + timedelta(minutes=2)

    repository.process_health(health(True, restored_at), config)

    assert repository.snapshot().state == NodeState.SYNCING
    assert collector.capture(restored_at) == (0, 0)
    assert reader.calls == calls_before_recovery
    with database.session() as db:
        event = db.scalars(select(LocalScadaEventRow)).one()
        assert event.is_open is False
        assert event.ended_at.replace(tzinfo=UTC) == restored_at


def test_non_utc_input_is_bucketed_and_serialized_as_utc(repository, base_time) -> None:
    enter_offline(repository, base_time)
    collector, _ = _collector(repository, [{"TEMP": 20.0}], {"TEMP": TagSpec(address="TEMP")})
    chile_time = (base_time + timedelta(hours=-4, seconds=27)).replace(
        tzinfo=timezone(timedelta(hours=-4))
    )
    collector.capture(chile_time)

    from collector_offline.config import SyncConfig

    records = repository.claim_backlog(base_time + timedelta(minutes=1), SyncConfig())
    assert records[0].payload["bucket"] == "2026-07-22T12:00:00+00:00"
    assert records[0].payload["captured_at"].endswith("+00:00")


def test_new_outage_rebaselines_discrete_state(repository, database, base_time) -> None:
    config = SupervisorConfig(failure_threshold=3, recovery_threshold=1)
    enter_offline(repository, base_time, config)
    first, _ = _collector(
        repository,
        [{"P001_ST": 0}],
        {"P001_ST": TagSpec(address="P001_ST", event=True)},
    )
    first.capture(base_time)
    with database.session() as db, db.begin():
        minute = db.scalars(select(LocalScadaMinuteRow)).one()
        minute.sync_status = "sent"
        minute.synced_at = base_time
    repository.process_health(health(True, base_time + timedelta(minutes=1)), config)
    assert repository.complete_sync_if_drained(base_time + timedelta(minutes=1)) is True

    enter_offline(repository, base_time + timedelta(minutes=2), config)
    second, _ = _collector(
        repository,
        [{"P001_ST": 1}],
        {"P001_ST": TagSpec(address="P001_ST", event=True)},
    )

    assert second.capture(base_time + timedelta(minutes=3))[1] == 0
    with database.session() as db:
        assert db.scalar(select(func.count()).select_from(LocalScadaEventRow)) == 0
