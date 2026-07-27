from __future__ import annotations

import random
import time
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

import requests
from requests.adapters import HTTPAdapter

from collector_offline.config import CentralConfig
from collector_offline.domain.models import (
    AckStatus,
    BackfillAck,
    BackfillRecord,
    BackfillResponse,
    HealthResult,
)
from collector_offline.domain.time import utc_now


class BackfillTransportError(RuntimeError):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class CentralClient:
    def __init__(
        self,
        config: CentralConfig,
        token: str,
        source_node_id: str,
        lagoon_id: str,
        product_type: str,
    ) -> None:
        self.config = config
        self.source_node_id = source_node_id
        self.lagoon_id = lagoon_id
        self.product_type = product_type
        self._session = requests.Session()
        self._session.mount(
            "https://",
            HTTPAdapter(pool_connections=2, pool_maxsize=4, max_retries=0, pool_block=True),
        )
        self._headers = {"Authorization": f"Bearer {token}"}
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

    def send_backfill(self, records: list[BackfillRecord]) -> BackfillResponse:
        correlation_id = str(uuid4())
        body = {
            "version": 1,
            "source_node_id": self.source_node_id,
            "lagoon_id": self.lagoon_id,
            "product_type": self.product_type,
            "minutes": [record.payload for record in records if record.kind == "minute"],
            "events": [record.payload for record in records if record.kind == "event"],
        }
        headers = {**self._headers, "X-Correlation-ID": correlation_id}
        try:
            response = self._session.post(
                self.config.sync_url,
                json=body,
                headers=headers,
                timeout=self.config.timeout_sec,
                verify=self._verify,
            )
        except requests.RequestException as exc:
            raise BackfillTransportError(
                f"central backfill request failed: {type(exc).__name__}: {exc}",
                retryable=True,
            ) from exc

        if response.status_code == 429 or response.status_code >= 500:
            raise BackfillTransportError(
                f"central backfill returned retryable HTTP {response.status_code}", retryable=True
            )
        if response.status_code >= 400:
            raise BackfillTransportError(
                f"central backfill returned permanent HTTP {response.status_code}", retryable=False
            )
        try:
            payload: dict[str, Any] = response.json()
            raw_results = payload["results"]
            if not isinstance(raw_results, list):
                raise TypeError("results is not a list")
            acknowledgements = [self._parse_ack(item) for item in raw_results]
        except (KeyError, TypeError, ValueError) as exc:
            raise BackfillTransportError(
                f"invalid central acknowledgement: {exc}", retryable=True
            ) from exc
        return BackfillResponse(
            acknowledgements=acknowledgements,
            correlation_id=str(payload.get("correlation_id") or correlation_id),
        )

    @staticmethod
    def _parse_ack(item: Any) -> BackfillAck:
        if not isinstance(item, dict):
            raise TypeError("result item is not an object")
        kind = str(item["kind"])
        if kind not in {"minute", "event"}:
            raise ValueError(f"invalid result kind {kind!r}")
        return BackfillAck(
            kind=kind,
            local_id=UUID(str(item["local_id"])),
            status=AckStatus(str(item["status"])),
            retryable=bool(item.get("retryable", False)),
            code=str(item["code"]) if item.get("code") else None,
            detail=str(item["detail"]) if item.get("detail") else None,
        )

    def close(self) -> None:
        self._session.close()
