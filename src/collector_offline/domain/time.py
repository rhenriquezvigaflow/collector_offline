from datetime import UTC, datetime


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("timestamp must be timezone-aware")
    return value.astimezone(UTC)


def minute_bucket(value: datetime) -> datetime:
    return ensure_utc(value).replace(second=0, microsecond=0)


def utc_now() -> datetime:
    return datetime.now(UTC)
