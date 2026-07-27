from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from typing import Protocol

from collector_offline.config import SyncConfig
from collector_offline.domain.models import BackfillRecord, BackfillResponse, NodeState
from collector_offline.infrastructure.http_client import BackfillTransportError
from collector_offline.infrastructure.repository import EdgeRepository

logger = logging.getLogger("collector_offline.sync")


class BackfillTransport(Protocol):
    def send_backfill(self, records: list[BackfillRecord]) -> BackfillResponse: ...


class SyncWorker:
    def __init__(
        self,
        repository: EdgeRepository,
        transport: BackfillTransport,
        config: SyncConfig,
    ) -> None:
        self.repository = repository
        self.transport = transport
        self.config = config

    def run_once(self, now: datetime) -> Counter[str]:
        if self.repository.snapshot().state != NodeState.SYNCING:
            return Counter()
        records = self.repository.claim_backlog(now, self.config)
        if not records:
            completed = self.repository.complete_sync_if_drained(now)
            return Counter({"sync_completed": 1}) if completed else Counter()
        correlation_id = None
        try:
            response = self.transport.send_backfill(records)
            correlation_id = response.correlation_id
            counts = self.repository.finalize_claim(records, response, now, self.config)
        except BackfillTransportError as exc:
            counts = self.repository.finalize_claim(
                records,
                None,
                now,
                self.config,
                transport_error=str(exc),
                transport_retryable=exc.retryable,
            )
            logger.warning(
                "Backfill batch failed",
                extra={"retryable": exc.retryable, "record_count": len(records), "error": str(exc)},
            )
        if counts:
            logger.info(
                "Backfill batch finalized",
                extra={"sync_counts": dict(counts), "correlation_id": correlation_id},
            )
        return counts
