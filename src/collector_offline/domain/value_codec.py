"""Codec compatible con ``crystal-backend/app/scada/value_codec.py``.

Se mantiene local para que el nodo siga siendo desplegable de manera independiente.
Las pruebas de contrato documentan la equivalencia con el backend central.
"""

from __future__ import annotations

from typing import Any

from collector_offline.domain.models import StorageValue

STATE_TAG_SUFFIXES = (".ST", "_ST", "_STS", "_ST_SCADA", "_STS_SCADA")
NUMERIC_TAG_SUFFIXES = ("_INT", "_DINT", "_REAL", "_FLOAT")
DEFAULT_STATE_MAX_VALUE = 3
TAG_STATE_MAX_VALUE = 11


def is_state_tag_id(tag_id: str | None) -> bool:
    normalized = str(tag_id or "").strip().upper()
    return bool(normalized) and any(normalized.endswith(suffix) for suffix in STATE_TAG_SUFFIXES)


def is_numeric_tag_id(tag_id: str | None) -> bool:
    normalized = str(tag_id or "").strip().upper()
    return bool(normalized) and any(normalized.endswith(suffix) for suffix in NUMERIC_TAG_SUFFIXES)


def coerce_state_value(value: Any, max_state: int = DEFAULT_STATE_MAX_VALUE) -> int | None:
    if isinstance(value, bool):
        normalized = int(value)
    elif isinstance(value, int):
        normalized = value
    elif isinstance(value, float):
        if not value.is_integer():
            return None
        normalized = int(value)
    elif isinstance(value, str):
        try:
            parsed = float(value.strip())
        except (TypeError, ValueError):
            return None
        if not parsed.is_integer():
            return None
        normalized = int(parsed)
    else:
        return None
    return normalized if 0 <= normalized <= max_state else None


def is_discrete(value: Any, tag_id: str | None = None) -> bool:
    if isinstance(value, bool):
        return True
    if is_numeric_tag_id(tag_id):
        return False
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1, 2, 3):
        return True
    return is_state_tag_id(tag_id) and coerce_state_value(value, TAG_STATE_MAX_VALUE) is not None


def to_storage_value(value: Any, tag_id: str | None = None) -> StorageValue:
    if value is None:
        return StorageValue()
    if (
        is_numeric_tag_id(tag_id)
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        return StorageValue(value_num=float(value))
    if is_state_tag_id(tag_id):
        state = coerce_state_value(value, TAG_STATE_MAX_VALUE)
        if state is not None:
            return StorageValue(state=state)
    if isinstance(value, int) and not isinstance(value, bool) and value in (0, 1, 2, 3):
        return StorageValue(state=value)
    if isinstance(value, bool):
        return StorageValue(value_bool=value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return StorageValue(value_num=float(value))
    return StorageValue()


def discrete_value(value: Any, tag_id: str) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if is_state_tag_id(tag_id):
        return coerce_state_value(value, TAG_STATE_MAX_VALUE)
    if isinstance(value, int) and value in (0, 1, 2, 3):
        return value
    return None
