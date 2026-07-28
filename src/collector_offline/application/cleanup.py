from __future__ import annotations

import logging
from datetime import datetime, timedelta

from collector_offline.application.chunk_builder import ChunkOutbox
from collector_offline.config import RetentionConfig
from collector_offline.infrastructure.chunk_repository import ChunkRepository
from collector_offline.infrastructure.repository import EdgeRepository

logger = logging.getLogger("collector_offline.cleanup")


class CleanupWorker:
    def __init__(
        self,
        repository: EdgeRepository,
        config: RetentionConfig,
        chunk_repository: ChunkRepository | None = None,
        outbox: ChunkOutbox | None = None,
    ) -> None:
        self.repository = repository
        self.config = config
        self.chunk_repository = chunk_repository
        self.outbox = outbox

    def run_once(self, now: datetime) -> dict[str, int]:
        if not self.config.enabled:
            return {
                "minutes": 0,
                "events": 0,
                "sessions": 0,
                "chunk_files": 0,
                "chunk_metadata": 0,
            }
        deleted = self.repository.cleanup(now, self.config)
        deleted["chunk_files"] = 0
        deleted["chunk_metadata"] = 0
        if self.chunk_repository is not None and self.outbox is not None:
            cutoff = now - timedelta(days=self.config.verified_files_days)
            for descriptor in self.chunk_repository.verified_files_before(cutoff):
                try:
                    self.outbox.delete_verified_files(descriptor)
                    self.chunk_repository.mark_file_deleted(descriptor.chunk_id, now)
                    deleted["chunk_files"] += 1
                except OSError:
                    logger.exception(
                        "Could not delete retained VERIFIED chunk",
                        extra={"chunk_id": str(descriptor.chunk_id)},
                    )
            deleted["chunk_metadata"] = self.chunk_repository.cleanup_verified_metadata(
                now,
                self.config,
            )
        if any(deleted.values()):
            logger.info("Retention cleanup completed", extra={"deleted": deleted})
        return deleted
