from __future__ import annotations

from pathlib import Path

from sqlalchemy import Engine, text


def apply_migrations(engine: Engine, migration_dir: Path) -> list[str]:
    applied: list[str] = []
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE IF NOT EXISTS edge_schema_migration ("
                "version varchar(255) PRIMARY KEY, applied_at timestamptz NOT NULL DEFAULT now())"
            )
        )
        existing = set(connection.scalars(text("SELECT version FROM edge_schema_migration")))
        for path in sorted(migration_dir.glob("*.sql")):
            if path.name in existing:
                continue
            connection.exec_driver_sql(path.read_text(encoding="utf-8"))
            connection.execute(
                text("INSERT INTO edge_schema_migration(version) VALUES (:version)"),
                {"version": path.name},
            )
            applied.append(path.name)
    return applied
