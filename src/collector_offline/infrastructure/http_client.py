from __future__ import annotations

import random
import time
from datetime import datetime

import requests
from requests.adapters import HTTPAdapter

from collector_offline.config import CentralConfig
from collector_offline.domain.models import HealthResult
from collector_offline.domain.time import utc_now


class CentralClient:
    def __init__(self, config: CentralConfig) -> None:
        self.config = config
        self._session = requests.Session()
        self._session.mount(
            "https://",
            HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0, pool_block=True),
        )
        self._verify: bool | str = config.ca_bundle or config.verify_tls

    def check_health(self, now: datetime | None = None) -> HealthResult:
        checked_at = now or utc_now()
        last_reason = "health check failed"
        last_status: int | None = None
        started = time.monotonic()
        for attempt in range(1, self.config.health_attempts + 1):
            try:
                response = self._session.get(
                    self.config.health_url,
                    timeout=self.config.timeout_sec,
                    verify=self._verify,
                )
                last_status = response.status_code
                if response.status_code == 200:
                    payload = response.json()
                    database_ok = (payload.get("checks") or {}).get("database") == "ok"
                    if payload.get("status") == "ready" and database_ok:
                        return HealthResult(
                            available=True,
                            checked_at=checked_at,
                            status_code=response.status_code,
                            latency_ms=round((time.monotonic() - started) * 1000, 1),
                        )
                    last_reason = "central readiness response is degraded"
                else:
                    last_reason = f"central health returned HTTP {response.status_code}"
            except (requests.RequestException, ValueError) as exc:
                last_reason = f"{type(exc).__name__}: {exc}"

            if attempt < self.config.health_attempts:
                delay = self.config.health_backoff_base_sec * (2 ** (attempt - 1))
                time.sleep(delay * random.uniform(0.8, 1.2))

        return HealthResult(
            available=False,
            checked_at=checked_at,
            reason=last_reason,
            status_code=last_status,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
        )

    def close(self) -> None:
        self._session.close()
