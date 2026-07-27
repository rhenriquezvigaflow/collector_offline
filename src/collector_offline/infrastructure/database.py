from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from collector_offline.config import DatabaseConfig

ADVISORY_LOCK_ID = 0x4352595354414C  # ASCII-ish "CRYSTAL", within signed bigint.


def build_engine(url: str, config: DatabaseConfig) -> Engine:
    kwargs: dict[str, object] = {
        "future": True,
        "pool_pre_ping": True,
    }
    if url.startswith("postgresql"):
        kwargs.update(
            pool_size=config.pool_size,
            connect_args={
                "options": (
                    f"-c statement_timeout={config.statement_timeout_ms} "
                    f"-c lock_timeout={config.lock_timeout_ms} "
                    "-c timezone=UTC"
                )
            },
        )
    return create_engine(url, **kwargs)


class Database:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._session_factory()
        try:
            yield session
        finally:
            session.close()


class InstanceLock:
    """Connection-scoped PostgreSQL advisory lock.

    SQLite is accepted only for automated tests; production startup rejects any
    second PostgreSQL process before it can read PLCs or claim backlog.
    """

    def __init__(self, engine: Engine, lock_id: int = ADVISORY_LOCK_ID) -> None:
        self._engine = engine
        self._lock_id = lock_id
        self._connection: Connection | None = None

    def acquire(self) -> bool:
        if self._engine.dialect.name != "postgresql":
            return True
        connection = self._engine.connect()
        acquired = bool(
            connection.execute(
                text("SELECT pg_try_advisory_lock(:lock_id)"), {"lock_id": self._lock_id}
            ).scalar_one()
        )
        if not acquired:
            connection.close()
        else:
            self._connection = connection
        return acquired

    def release(self) -> None:
        if self._connection is None:
            return
        connection = self._connection
        try:
            connection.execute(
                text("SELECT pg_advisory_unlock(:lock_id)"), {"lock_id": self._lock_id}
            )
        finally:
            connection.close()
            self._connection = None

    def __enter__(self) -> InstanceLock:
        if not self.acquire():
            raise RuntimeError("another Collector Offline instance already owns the node lock")
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()
