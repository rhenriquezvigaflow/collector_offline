from __future__ import annotations

import json
import sys
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import requests
from sqlalchemy import create_engine, text

from collector_offline.application.collector import LocalCollector
from collector_offline.config import SyncConfig, TagSpec, load_config
from collector_offline.infrastructure.database import Database
from collector_offline.infrastructure.drivers import BoundReader, SimulatorReader
from collector_offline.infrastructure.repository import EdgeRepository

config, secrets = load_config("/fixtures/site.yml")
engine = create_engine(secrets.database_url)
repository = EdgeRepository(Database(engine), "edge-docker-test", "docker-test")


def central(path: str, payload: dict | None = None) -> dict:
    response = (
        requests.get(f"https://central:8090{path}", verify="/tls/central.crt", timeout=5)
        if payload is None
        else requests.post(
            f"https://central:8090{path}", json=payload, verify="/tls/central.crt", timeout=5
        )
    )
    response.raise_for_status()
    return response.json()


def wait_for(predicate, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.5)
    raise AssertionError("Timed out waiting for expected state")


def counts() -> dict:
    with engine.connect() as connection:
        return dict(
            connection.execute(
                text("SELECT sync_status, count(*) FROM local_scada_minute GROUP BY sync_status")
            ).all()
        )


command = sys.argv[1]
if command == "outage":
    assert repository.snapshot().state == "online"
    assert counts() == {}  # ONLINE did not read the simulator.
    central("/control", {"ready": False})
    wait_for(lambda: repository.snapshot().state == "offline" and counts().get("pending", 0) >= 2)
    # Use the real collector/repository to seed a state change and an expired lease.
    now = datetime.now(UTC)
    reader = SimulatorReader({"TEST_RUN": False, "TEST_EXTRA": 123.5})
    collector = LocalCollector(
        repository,
        [
            BoundReader(
                "isolated",
                reader,
                {
                    "TEST_RUN": TagSpec(address="TEST_RUN", event=True),
                    "TEST_EXTRA": TagSpec(address="TEST_EXTRA"),
                },
            )
        ],
        "docker-test",
    )
    collector.capture(now)
    repository.claim_backlog(datetime.now(UTC) - timedelta(seconds=30), SyncConfig(lease_sec=10))
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM local_scada_event")) >= 1
        assert (
            connection.scalar(
                text("SELECT count(*) FROM local_scada_minute WHERE sync_status='syncing'")
            )
            >= 2
        )
elif command == "persisted":
    assert repository.snapshot().state == "offline"
    assert sum(counts().values()) >= 3
    assert counts().get("pending", 0) >= 3  # Expired leases recovered on startup.
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT count(*) FROM local_scada_event")) >= 1
        assert connection.scalar(text("SELECT count(*) FROM edge_tag_state")) >= 1
elif command == "recover":
    central("/control", {"ready": True})
    wait_for(lambda: repository.snapshot().state == "online", timeout=120)
    metrics = central("/metrics")
    assert metrics["posts"] >= 2 and metrics["duplicates"] >= 1
    assert metrics["forbidden"] == 0
    assert set(counts()) == {"sent"}
    with engine.connect() as connection:
        assert metrics["minutes"] == connection.scalar(
            text("SELECT count(*) FROM local_scada_minute")
        )
        assert metrics["events"] == connection.scalar(
            text("SELECT count(*) FROM local_scada_event")
        )
        assert connection.scalar(text("SELECT count(*) FROM local_scada_event WHERE is_open")) == 0
    print(json.dumps(metrics))
elif command == "volume":
    # Optional, caller has stopped collector and verified host resources.
    session_id = uuid4()
    started = time.monotonic()
    with engine.begin() as connection:
        connection.execute(
            text("""
            INSERT INTO local_outage_session
            (id,source_node_id,lagoon_id,started_at,connectivity_restored_at,status)
            VALUES (:id,'edge-docker-test','docker-test',now()-interval '20 days',now(),'syncing')
        """),
            {"id": session_id},
        )
        connection.execute(
            text("""
            INSERT INTO local_scada_minute
            (id,outage_session_id,source_node_id,lagoon_id,tag_id,bucket,value_num,captured_at)
            SELECT md5('volume-' || i)::uuid,:id,'edge-docker-test','docker-test',
                   'VOLUME_' || (i % 50),
                   date_trunc('minute',now())-interval '20 days'+(i/50)*interval '1 minute',
                   i::float, now()
            FROM generate_series(0,499999) AS i
        """),
            {"id": session_id},
        )
        connection.execute(
            text("UPDATE edge_runtime_state SET state='syncing',current_session_id=:id"),
            {"id": session_id},
        )
        size = connection.scalar(text("SELECT pg_database_size(current_database())"))
    print(
        json.dumps(
            {
                "inserted": 500000,
                "insert_seconds": time.monotonic() - started,
                "database_bytes": size,
            }
        )
    )
elif command == "volume-verify":
    wait_for(lambda: repository.snapshot().state == "online", timeout=7200)
    metrics = central("/metrics")
    with engine.connect() as connection:
        count = connection.scalar(
            text(
                "SELECT count(*) FROM local_scada_minute "
                "WHERE tag_id LIKE 'VOLUME_%' AND sync_status='sent'"
            )
        )
        assert count == 500000
        assert metrics["minutes"] == connection.scalar(
            text("SELECT count(*) FROM local_scada_minute")
        )
        assert metrics["value_sum"] == connection.scalar(
            text("SELECT sum(value_num) FROM local_scada_minute")
        )
        assert (
            connection.scalar(
                text("SELECT sum(value_num) FROM local_scada_minute WHERE tag_id LIKE 'VOLUME_%'")
            )
            == 499999 * 500000 / 2
        )
    print(json.dumps({"verified_sent": count, "central": metrics}))
else:
    raise SystemExit("Unknown scenario")
print(json.dumps({"scenario": command, "result": "passed", "counts": counts()}))
