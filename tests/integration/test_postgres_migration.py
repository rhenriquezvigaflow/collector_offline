from __future__ import annotations

import os
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from collector_offline.infrastructure.database import InstanceLock
from collector_offline.migrations import apply_migrations


@pytest.mark.postgres
def test_real_postgres_migration_and_instance_lock() -> None:
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    engine = create_engine(url, future=True)
    migration_dir = Path(__file__).parents[2] / "migrations" / "local"
    apply_migrations(engine, migration_dir)

    with engine.connect() as connection:
        tables = set(
            connection.scalars(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname='public' "
                    "AND tablename LIKE 'local_%'"
                )
            )
        )
    assert {"local_outage_session", "local_scada_minute", "local_scada_event"} <= tables

    first = InstanceLock(engine)
    second = InstanceLock(engine)
    assert first.acquire() is True
    try:
        assert second.acquire() is False
    finally:
        first.release()
        second.release()
        engine.dispose()
