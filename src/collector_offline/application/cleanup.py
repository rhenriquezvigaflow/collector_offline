from __future__ import annotations

import logging
from datetime import datetime

from collector_offline.config import RetentionConfig
from collector_offline.infrastructure.repository import EdgeRepository

logger = logging.getLogger("collector_offline.cleanup")


class CleanupWorker:
    def __init__(self, repository: EdgeRepository, config: RetentionConfig) -> None:
        self.repository = repository
        self.config = config

    def run_once(self, now: datetime) -> dict[str, int]:
        if not self.config.enabled:
            return {"minutes": 0, "events": 0, "sessions": 0}
        deleted = self.repository.cleanup(now, self.config)
        if any(deleted.values()):
            logger.info("Retention cleanup completed", extra={"deleted": deleted})
        return deleted
