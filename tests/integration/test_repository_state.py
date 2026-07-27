from __future__ import annotations

from datetime import timedelta

from conftest import enter_offline, health
from sqlalchemy import func, select

from collector_offline.config import SupervisorConfig
from collector_offline.domain.models import NodeState
from collector_offline.infrastructure.models import OutageSessionRow


def test_online_stable_and_microcuts_do_not_open_session(repository, database, base_time) -> None:
    config = SupervisorConfig(failure_threshold=3, recovery_threshold=2)
    repository.process_health(health(True, base_time), config)
    repository.process_health(health(False, base_time + timedelta(seconds=60)), config)
    repository.process_health(health(False, base_time + timedelta(seconds=120)), config)

    assert repository.snapshot().state == NodeState.ONLINE
    with database.session() as db:
        assert db.scalar(select(func.count()).select_from(OutageSessionRow)) == 0


def test_third_failure_opens_exactly_one_session(repository, database, base_time) -> None:
    config = SupervisorConfig(failure_threshold=3, recovery_threshold=2)
    enter_offline(repository, base_time, config)
    session_id = repository.snapshot().current_session_id
    repository.process_health(health(False, base_time + timedelta(minutes=1)), config)

    assert repository.snapshot().state == NodeState.OFFLINE
    assert repository.snapshot().current_session_id == session_id
    with database.session() as db:
        assert db.scalar(select(func.count()).select_from(OutageSessionRow)) == 1


def test_restart_recovers_offline_state(repository, database, base_time) -> None:
    enter_offline(repository, base_time)
    session_id = repository.snapshot().current_session_id

    from collector_offline.infrastructure.repository import EdgeRepository

    restarted = EdgeRepository(database, "edge-test-01", "small_sim")
    snapshot = restarted.initialize_runtime()

    assert snapshot.state == NodeState.OFFLINE
    assert snapshot.current_session_id == session_id


def test_new_failure_during_syncing_returns_to_same_outage(repository, base_time) -> None:
    config = SupervisorConfig(
        failure_threshold=3, recovery_threshold=1, syncing_failure_threshold=1
    )
    enter_offline(repository, base_time, config)
    session_id = repository.snapshot().current_session_id
    repository.process_health(health(True, base_time + timedelta(minutes=1)), config)
    assert repository.snapshot().state == NodeState.SYNCING

    repository.process_health(health(False, base_time + timedelta(minutes=2)), config)

    assert repository.snapshot().state == NodeState.OFFLINE
    assert repository.snapshot().current_session_id == session_id
