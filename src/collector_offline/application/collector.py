from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any

from collector_offline.domain.models import DataQuality, NodeState, StorageValue, TagObservation
from collector_offline.domain.time import ensure_utc, minute_bucket
from collector_offline.domain.value_codec import (
    coerce_state_value,
    discrete_value,
    is_discrete,
    to_storage_value,
)
from collector_offline.infrastructure.drivers import BoundReader
from collector_offline.infrastructure.repository import EdgeRepository

logger = logging.getLogger("collector_offline.collector")


class LocalCollector:
    def __init__(
        self,
        repository: EdgeRepository,
        readers: list[BoundReader],
        lagoon_id: str,
    ) -> None:
        self.repository = repository
        self.readers = readers
        self.lagoon_id = lagoon_id

    def capture(self, captured_at: datetime) -> tuple[int, int]:
        captured_at = ensure_utc(captured_at)
        if self.repository.snapshot().state != NodeState.OFFLINE:
            return (0, 0)
        bucket = minute_bucket(captured_at)
        observations_by_tag: dict[str, TagObservation] = {}
        discrete: dict[str, tuple[int, str | None]] = {}

        for binding in self.readers:
            try:
                values = binding.reader.read_once()
            except Exception as exc:
                logger.error(
                    "PLC read failed",
                    extra={"plc": binding.name, "error": str(exc)},
                )
                continue

            for tag_id, spec in binding.tag_specs.items():
                if tag_id not in values or values[tag_id] is None:
                    logger.warning(
                        "Tag read unavailable",
                        extra={"plc": binding.name, "tag_id": tag_id},
                    )
                    continue
                raw_value = values[tag_id]
                try:
                    normalized = self._normalize(raw_value, spec.data_type, tag_id)
                except (TypeError, ValueError) as exc:
                    logger.warning(
                        "Tag value rejected",
                        extra={
                            "plc": binding.name,
                            "tag_id": tag_id,
                            "error": str(exc),
                        },
                    )
                    continue
                if not normalized.has_value():
                    logger.warning(
                        "Tag type is unsupported by central storage",
                        extra={"plc": binding.name, "tag_id": tag_id},
                    )
                    continue
                observations_by_tag[tag_id] = TagObservation(
                    lagoon_id=self.lagoon_id,
                    tag_id=tag_id,
                    bucket=bucket,
                    captured_at=captured_at,
                    value=normalized,
                    quality=DataQuality.GOOD,
                )
                event_enabled = spec.event is True or (
                    spec.event is None and is_discrete(raw_value, tag_id)
                )
                if event_enabled:
                    state = discrete_value(raw_value, tag_id)
                    if state is not None:
                        discrete[tag_id] = (state, spec.label)

        return self.repository.persist_capture(observations_by_tag.values(), discrete)

    @staticmethod
    def _normalize(value: Any, data_type: str, tag_id: str) -> StorageValue:
        if data_type == "auto":
            normalized = to_storage_value(value, tag_id)
        elif data_type == "bool":
            if isinstance(value, bool):
                normalized = to_storage_value(value, tag_id=None)
            elif value in (0, 1):
                normalized = to_storage_value(bool(value), tag_id=None)
            else:
                raise ValueError("expected bool or 0/1")
        elif data_type == "state":
            state = coerce_state_value(value, max_state=11)
            if state is None:
                raise ValueError("expected integral state in range 0..11")
            normalized = StorageValue(state=state)
        elif data_type == "int":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("expected integer")
            integer = int(value)
            if float(value) != integer:
                raise ValueError("expected integral numeric value")
            normalized = StorageValue(value_num=float(integer))
        elif data_type == "float":
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("expected numeric value")
            normalized = StorageValue(value_num=float(value))
        else:
            raise ValueError(f"unsupported data type {data_type}")

        if normalized.value_num is not None and not math.isfinite(normalized.value_num):
            raise ValueError("numeric value must be finite")
        return normalized

    def close(self) -> None:
        for binding in self.readers:
            binding.reader.close()
