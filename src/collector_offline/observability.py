from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any


class JsonFormatter(logging.Formatter):
    _reserved = set(logging.makeLogRecord({}).__dict__) | {"message", "asctime"}

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "component": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key not in self._reserved and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False, separators=(",", ":"))


class RedactingFormatter(logging.Formatter):
    def __init__(self, wrapped: logging.Formatter, secrets: Iterable[str]) -> None:
        super().__init__()
        self.wrapped = wrapped
        self.secrets = sorted({value for value in secrets if value}, key=len, reverse=True)

    def format(self, record: logging.LogRecord) -> str:
        output = self.wrapped.format(record)
        for value in self.secrets:
            output = output.replace(json.dumps(value, ensure_ascii=False)[1:-1], "[redacted]")
            output = output.replace(value, "[redacted]")
        return output


def configure_logging(
    level: str, json_output: bool, source_node_id: str, secrets: Iterable[str] = ()
) -> None:
    handler = logging.StreamHandler()
    if json_output:
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
    assert handler.formatter is not None
    handler.setFormatter(RedactingFormatter(handler.formatter, secrets))
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    old_factory = logging.getLogRecordFactory()

    def record_factory(*args: Any, **kwargs: Any) -> logging.LogRecord:
        record = old_factory(*args, **kwargs)
        record.source_node_id = source_node_id
        return record

    logging.setLogRecordFactory(record_factory)
    for noisy in ("urllib3", "opcua", "pycomm3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
