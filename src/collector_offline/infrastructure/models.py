from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
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
    text,
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
    next_chunk_sequence: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
        default=1,
        server_default="1",
    )

    __table_args__ = (
        CheckConstraint("id = 1", name="ck_edge_runtime_singleton"),
        CheckConstraint("state IN ('online','offline','syncing')", name="ck_edge_runtime_state"),
        CheckConstraint("next_chunk_sequence > 0", name="ck_edge_runtime_chunk_sequence"),
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
    chunk_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("transfer_chunk.chunk_id", ondelete="SET NULL")
    )


class LocalScadaMinuteRow(SyncColumns, Base):
    __tablename__ = "local_scada_minute"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    outage_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("local_outage_session.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="collector_offline",
        server_default="collector_offline",
    )
    lagoon_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tag_id: Mapped[str] = mapped_column(String(64), nullable=False)
    bucket: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[int | None] = mapped_column(Integer)
    value_num: Mapped[float | None] = mapped_column(Float)
    value_bool: Mapped[bool | None] = mapped_column(Boolean)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("lagoon_id", "tag_id", "bucket", name="uq_local_scada_minute"),
        CheckConstraint(
            "sync_status IN ('pending','syncing','chunked','sent','failed')",
            name="ck_local_scada_minute_sync_status",
        ),
        CheckConstraint(
            "source = 'collector_offline'",
            name="ck_local_scada_minute_source",
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
        Index("ix_local_scada_minute_chunk", "chunk_id"),
    )


class LocalScadaEventRow(SyncColumns, Base):
    __tablename__ = "local_scada_event"

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    outage_session_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("local_outage_session.id", ondelete="RESTRICT"),
        nullable=False,
    )
    source: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="collector_offline",
        server_default="collector_offline",
    )
    lagoon_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tag_id: Mapped[str] = mapped_column(String(64), nullable=False)
    tag_label: Mapped[str | None] = mapped_column(String(255))
    previous_state: Mapped[int | None] = mapped_column(Integer)
    state: Mapped[int] = mapped_column(Integer, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "sync_status IN ('pending','syncing','chunked','sent','failed')",
            name="ck_local_scada_event_sync_status",
        ),
        CheckConstraint("ended_at IS NULL OR ended_at >= started_at", name="ck_event_end"),
        CheckConstraint("source = 'collector_offline'", name="ck_local_scada_event_source"),
        Index(
            "ix_local_scada_event_pending",
            "sync_status",
            "next_retry_at",
            "started_at",
        ),
        Index("ix_local_scada_event_session", "outage_session_id", "started_at"),
        Index("ix_local_scada_event_chunk", "chunk_id"),
        Index(
            "uq_local_scada_event_open_tag",
            "lagoon_id",
            "tag_id",
            unique=True,
            sqlite_where=text("ended_at IS NULL"),
            postgresql_where=text("ended_at IS NULL"),
        ),
    )

    @property
    def is_open(self) -> bool:
        """Compatibility view; openness is represented only by ended_at IS NULL."""
        return self.ended_at is None


class TransferChunkRow(Base):
    __tablename__ = "transfer_chunk"

    chunk_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)
    transfer_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, unique=True, default=uuid4
    )
    sequence_number: Mapped[int] = mapped_column(BigInteger, nullable=False, unique=True)
    source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="collector_offline", server_default="collector_offline"
    )
    source_node_id: Mapped[str] = mapped_column(String(128), nullable=False)
    lagoon_id: Mapped[str] = mapped_column(String(64), nullable=False)
    data_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    file_path: Mapped[str | None] = mapped_column(Text)
    manifest_path: Mapped[str | None] = mapped_column(Text)
    from_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    to_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    uncompressed_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    compressed_size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    sha256: Mapped[str | None] = mapped_column(String(64))
    part_size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    total_parts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_parts: Mapped[list[int]] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="CREATING")
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_token: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    staged_rows: Mapped[int | None] = mapped_column(Integer)
    inserted_rows: Mapped[int | None] = mapped_column(Integer)
    duplicate_rows: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    sealed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    file_deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("source = 'collector_offline'", name="ck_transfer_chunk_source"),
        CheckConstraint("data_kind IN ('minute','event')", name="ck_transfer_chunk_data_kind"),
        CheckConstraint(
            "status IN ('CREATING','SEALED','PENDING','UPLOADING','UPLOADED',"
            "'IMPORTING','VERIFIED','FAILED','QUARANTINED')",
            name="ck_transfer_chunk_status",
        ),
        CheckConstraint("row_count > 0", name="ck_transfer_chunk_rows"),
        CheckConstraint("part_size_bytes > 0", name="ck_transfer_chunk_part_size"),
        Index("ix_transfer_chunk_status_retry", "status", "next_retry_at", "sequence_number"),
        Index("ix_transfer_chunk_verified", "verified_at"),
    )


class EdgeTagStateRow(Base):
    __tablename__ = "edge_tag_state"

    lagoon_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    tag_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_value: Mapped[int] = mapped_column(Integer, nullable=False)
    open_event_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("local_scada_event.id", ondelete="SET NULL")
    )
