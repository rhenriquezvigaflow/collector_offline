from __future__ import annotations

import logging
import signal
import threading
import time
from datetime import UTC, datetime

from collector_offline.application.cleanup import CleanupWorker
from collector_offline.application.collector import LocalCollector
from collector_offline.application.state_machine import StateCoordinator
from collector_offline.application.sync_worker import SyncWorker
from collector_offline.config import AppConfig
from collector_offline.domain.models import NodeState
from collector_offline.domain.time import minute_bucket
from collector_offline.infrastructure.http_client import CentralClient
from collector_offline.infrastructure.repository import EdgeRepository
from collector_offline.status_server import StatusServer

logger = logging.getLogger("collector_offline.runtime")


class EdgeRuntime:
    def __init__(
        self,
        config: AppConfig,
        repository: EdgeRepository,
        coordinator: StateCoordinator,
        collector: LocalCollector,
        sync_worker: SyncWorker,
        cleanup_worker: CleanupWorker,
        central_client: CentralClient,
    ) -> None:
        self.config = config
        self.repository = repository
        self.coordinator = coordinator
        self.collector = collector
        self.sync_worker = sync_worker
        self.cleanup_worker = cleanup_worker
        self.central_client = central_client
        self.stop_event = threading.Event()
        self.status_server: StatusServer | None = None

    def request_stop(self, *_args: object) -> None:
        logger.info("Shutdown requested")
        self.stop_event.set()

    def run(self) -> None:
        snapshot = self.repository.initialize_runtime()
        self.repository.recover_expired_leases(datetime.now(UTC))
        logger.info(
            "Runtime initialized",
            extra={
                "state": snapshot.state.value,
                "outage_session_id": (
                    str(snapshot.current_session_id) if snapshot.current_session_id else None
                ),
            },
        )
        if self.config.status_server.enabled:
            self.status_server = StatusServer(
                self.config.status_server.host,
                self.config.status_server.port,
                self.repository.operational_status,
            )
            self.status_server.start()

        self._install_signal_handlers()
        next_health = 0.0
        next_sync = 0.0
        next_cleanup = time.monotonic() + self.config.retention.interval_sec
        last_capture_bucket = None
        try:
            while not self.stop_event.is_set():
                monotonic_now = time.monotonic()
                wall_now = datetime.now(UTC)

                if monotonic_now >= next_health:
                    health = self.central_client.check_health(wall_now)
                    transition = self.coordinator.observe_health(health)
                    next_health = monotonic_now + self.config.supervisor.health_interval_sec
                    if (
                        transition.after == NodeState.OFFLINE
                        and transition.before != NodeState.OFFLINE
                    ):
                        last_capture_bucket = None

                state = self.repository.snapshot().state
                current_bucket = minute_bucket(wall_now)
                capture_aligned = (
                    int(current_bucket.timestamp()) % self.config.capture.interval_sec == 0
                )
                if (
                    state == NodeState.OFFLINE
                    and capture_aligned
                    and current_bucket != last_capture_bucket
                ):
                    minutes, events = self.collector.capture(wall_now)
                    last_capture_bucket = current_bucket
                    logger.info(
                        "Offline capture completed",
                        extra={
                            "bucket": current_bucket.isoformat(),
                            "minute_rows": minutes,
                            "event_rows": events,
                        },
                    )
                elif state == NodeState.SYNCING and monotonic_now >= next_sync:
                    self.sync_worker.run_once(wall_now)
                    next_sync = monotonic_now + self.config.sync.idle_interval_sec

                if monotonic_now >= next_cleanup:
                    self.cleanup_worker.run_once(wall_now)
                    next_cleanup = monotonic_now + self.config.retention.interval_sec

                self.stop_event.wait(self.config.supervisor.runtime_tick_sec)
        finally:
            self.close()

    def _install_signal_handlers(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)

    def close(self) -> None:
        self.collector.close()
        self.central_client.close()
        if self.status_server:
            self.status_server.close()
        logger.info("Runtime stopped cleanly")
