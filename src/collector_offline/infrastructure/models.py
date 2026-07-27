from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class RuntimeStateRow(Base):
    __tablename__ = "edge_runtime_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    state: Mapped[str] = mapped_column(String(16), nullable=False, default="online")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_session_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True)
    last_health_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_health_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_edge_runtime_singleton"),
        CheckConstraint("state IN ('online','offline','syncing')", name="ck_edge_runtime_state"),
    )


class OutageSessionRow(Base):
    __tablename__ = "local_outage_session"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    source_node_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    lagoon_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    connectivity_restored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sync_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_reason: Mapped[str | None] = mapped_column(Text)
    diagnostic_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('offline','syncing','completed')", name="ck_local_outage_session_status"
        ),
        Index("ix_local_outage_session_status_started", "status", "started_at"),
    )


class SyncColumns:
    sync_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    sync_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))


class LocalScadaMinuteRow(SyncColumns, Base):
    __tablename__ = "local_scada_minute"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    outage_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("local_outage_session.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lagoon_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tag_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[int | None] = mapped_column(Integer)
    value_num: Mapped[float | None] = mapped_column(Float)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    quality: Mapped[str] = mapped_column(String(16), nullable=False, default="good")
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (
        UniqueConstraint("lagoon_id", "tag_id", "bucket", name="uq_local_scada_minute"),
        CheckConstraint(
            "sync_status IN ('pending','syncing','sent','failed')",
            name="ck_local_scada_minute_sync_status",
        ),
        CheckConstraint(
            "quality IN ('good','bad','uncertain')", name="ck_local_scada_minute_quality"
        ),
        CheckConstraint(
            "(CASE WHEN state IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN value_num IS NULL THEN 0 ELSE 1 END + "
            "CASE WHEN value_bool IS NULL THEN 0 ELSE 1 END) = 1",
            name="ck_local_scada_minute_one_value",
        ),
        Index(
            "ix_local_scada_minute_pending",
            "sync_status",
            "next_retry_at",
            "bucket",
        ),
        Index("ix_local_scada_minute_session", "outage_session_id", "bucket"),
    )


class LocalScadaEventRow(SyncColumns, Base):
    __tablename__ = "local_scada_event"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    outage_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("local_outage_session.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source_node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lagoon_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tag_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tag_label: Mapped[str | None] = mapped_column(String(255))
    alert_type: Mapped[str] = mapped_column(String(32), nullable=False, default="STATE_CHANGE")
    previous_state: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_open: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (
        CheckConstraint(
            "sync_status IN ('pending','syncing','sent','failed')",
            name="ck_local_scada_event_sync_status",
        ),
        CheckConstraint(
            "(is_open AND ended_at IS NULL) OR (NOT is_open AND ended_at IS NOT NULL)",
            name="ck_local_scada_event_open",
        ),
        Index(
            "ix_local_scada_event_pending",
            "sync_status",
            "next_retry_at",
            "started_at",
        ),
        Index("ix_local_scada_event_session", "outage_session_id", "started_at"),
        Index(
            "ix_local_scada_event_open_tag",
            "lagoon_id",
            "tag_id",
            "is_open",
        ),
    )


class EdgeTagStateRow(Base):
    __tablename__ = "edge_tag_state"

    lagoon_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)
    last_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    open_event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("local_scada_event.id", ondelete="SET NULL")
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    __table_args__ = (Index("ix_edge_tag_state_observed", "last_observed_at"),)
