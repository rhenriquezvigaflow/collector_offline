from __future__ import annotations

import logging

from collector_offline.config import SupervisorConfig
from collector_offline.domain.models import HealthResult, TransitionResult
from collector_offline.infrastructure.repository import EdgeRepository

logger = logging.getLogger("collector_offline.supervisor")


class StateCoordinator:
    """Único punto autorizado para cambiar ONLINE/OFFLINE/SYNCING."""

    def __init__(self, repository: EdgeRepository, config: SupervisorConfig) -> None:
        self.repository = repository
        self.config = config

    def observe_health(self, health: HealthResult) -> TransitionResult:
        transition = self.repository.process_health(health, self.config)
        if transition.changed:
            logger.warning(
                "Node state transition",
                extra={
                    "state_before": transition.before.value,
                    "state_after": transition.after.value,
                    "outage_session_id": (
                        str(transition.session_id) if transition.session_id else None
                    ),
                    "reason": transition.reason,
                },
            )
        return transition
