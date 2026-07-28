from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast
from uuid import UUID, uuid4

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.orm import Session

from collector_offline.config import ChunkConfig, RetentionConfig, TransferConfig
from collector_offline.domain.models import (
    HISTORICAL_SOURCE,
    ChunkStatus,
    DataKind,
    SyncStatus,
    require_chunk_transition,
)
from collector_offline.domain.transfer import (
    ChunkBuildClaim,
    ChunkDescriptor,
    CsvRecord,
    SealedChunk,
    csv_header,
    csv_scalar,
    encode_csv_row,
    utc_text,
)
from collector_offline.infrastructure.database import Database
from collector_offline.infrastructure.models import (
    LocalScadaEventRow,
    LocalScadaMinuteRow,
    RuntimeStateRow,
    TransferChunkRow,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _error(value: str | None) -> str | None:
    return value[:2000] if value else None


class ChunkRepository:
    def __init__(self, database: Database, source_node_id: str, lagoon_id: str) -> None:
        self.database = database
        self.source_node_id = source_node_id
        self.lagoon_id = lagoon_id

    @staticmethod
    def _record(data_kind: DataKind, row: LocalScadaMinuteRow | LocalScadaEventRow) -> CsvRecord:
        if row.source != HISTORICAL_SOURCE:
            raise ValueError("local historical source must be collector_offline")
        if data_kind == DataKind.MINUTE:
            assert isinstance(row, LocalScadaMinuteRow)
            timestamp = _as_utc(row.bucket)
            values: tuple[str, ...] = (
                row.lagoon_id,
                row.tag_id,
                utc_text(timestamp),
                csv_scalar(row.state),
                csv_scalar(row.value_num),
                csv_scalar(row.value_bool),
                HISTORICAL_SOURCE,
            )
        else:
            assert isinstance(row, LocalScadaEventRow)
            timestamp = _as_utc(row.started_at)
            ended_at = _as_utc(row.ended_at) if row.ended_at else None
            duration = (
                max(0, int((ended_at - timestamp).total_seconds())) if ended_at else None
            )
            values = (
                str(row.id),
                row.lagoon_id,
                row.tag_id,
                csv_scalar(row.tag_label),
                "STATE_CHANGE",
                csv_scalar(row.previous_state),
                csv_scalar(row.state),
                utc_text(timestamp),
                utc_text(ended_at) if ended_at else "",
                csv_scalar(duration),
                HISTORICAL_SOURCE,
            )
        return CsvRecord(local_id=row.id, timestamp=timestamp, values=values)

    def claim_for_build(
        self,
        data_kind: DataKind,
        now: datetime,
        chunk_config: ChunkConfig,
        part_size_bytes: int,
        *,
        force: bool = False,
    ) -> ChunkBuildClaim | None:
        now = _as_utc(now)
        model = LocalScadaMinuteRow if data_kind == DataKind.MINUTE else LocalScadaEventRow
        timestamp_column = (
            LocalScadaMinuteRow.bucket
            if data_kind == DataKind.MINUTE
            else LocalScadaEventRow.started_at
        )
        with self.database.session() as db, db.begin():
            query = (
                select(model)
                .where(
                    model.sync_status == SyncStatus.PENDING.value,
                    model.chunk_id.is_(None),
                    or_(model.next_retry_at.is_(None), model.next_retry_at <= now),
                )
                .order_by(timestamp_column, model.tag_id, model.id)
                .limit(chunk_config.max_rows)
                .with_for_update(skip_locked=True)
            )
            if data_kind == DataKind.EVENT:
                query = query.where(LocalScadaEventRow.ended_at.is_not(None))
            rows = [
                cast(LocalScadaMinuteRow | LocalScadaEventRow, row)
                for row in db.execute(query).scalars()
            ]
            if not rows:
                return None

            selected_rows: list[LocalScadaMinuteRow | LocalScadaEventRow] = []
            records: list[CsvRecord] = []
            size = len(encode_csv_row(csv_header(data_kind)))
            size_reached = False
            for row in rows:
                record = self._record(data_kind, row)
                row_size = len(encode_csv_row(record.values))
                if records and size + row_size > chunk_config.max_uncompressed_size_bytes:
                    size_reached = True
                    break
                selected_rows.append(row)
                records.append(record)
                size += row_size
                if size >= chunk_config.max_uncompressed_size_bytes:
                    size_reached = True
                    break

            oldest_age = now - records[0].timestamp
            age_reached = oldest_age >= timedelta(minutes=chunk_config.max_age_minutes)
            count_reached = len(records) >= chunk_config.max_rows
            if not force and not (age_reached or count_reached or size_reached):
                return None

            runtime = db.execute(
                select(RuntimeStateRow)
                .where(RuntimeStateRow.id == 1)
                .with_for_update()
            ).scalar_one()
            sequence = runtime.next_chunk_sequence
            runtime.next_chunk_sequence += 1
            chunk_id = uuid4()
            transfer_id = uuid4()
            lease_token = uuid4()
            lease_until = now + timedelta(seconds=chunk_config.claim_timeout_seconds)
            chunk = TransferChunkRow(
                chunk_id=chunk_id,
                transfer_id=transfer_id,
                sequence_number=sequence,
                source=HISTORICAL_SOURCE,
                source_node_id=self.source_node_id,
                lagoon_id=self.lagoon_id,
                data_kind=data_kind.value,
                from_timestamp=records[0].timestamp,
                to_timestamp=records[-1].timestamp,
                row_count=len(records),
                part_size_bytes=part_size_bytes,
                confirmed_parts=[],
                status=ChunkStatus.CREATING.value,
                lease_until=lease_until,
                lease_token=lease_token,
                created_at=now,
            )
            db.add(chunk)
            db.flush()
            for row in selected_rows:
                row.chunk_id = chunk_id
                row.sync_status = SyncStatus.SYNCING.value
                row.lease_until = lease_until
                row.lease_token = lease_token
                row.last_sync_error = None

            return ChunkBuildClaim(
                chunk_id=chunk_id,
                transfer_id=transfer_id,
                sequence_number=sequence,
                data_kind=data_kind,
                source_node_id=self.source_node_id,
                lagoon_id=self.lagoon_id,
                part_size_bytes=part_size_bytes,
                records=tuple(records),
                lease_token=lease_token,
            )

    def seal(
        self,
        claim: ChunkBuildClaim,
        sealed: SealedChunk,
        now: datetime,
    ) -> None:
        now = _as_utc(now)
        with self.database.session() as db, db.begin():
            chunk = db.execute(
                select(TransferChunkRow)
                .where(TransferChunkRow.chunk_id == claim.chunk_id)
                .with_for_update()
            ).scalar_one()
            if (
                chunk.status != ChunkStatus.CREATING.value
                or chunk.lease_token != claim.lease_token
            ):
                raise RuntimeError("chunk creation lease was lost")
            require_chunk_transition(chunk.status, ChunkStatus.SEALED)
            chunk.file_path = str(sealed.file_path)
            chunk.manifest_path = str(sealed.manifest_path)
            chunk.uncompressed_size_bytes = sealed.uncompressed_size_bytes
            chunk.compressed_size_bytes = sealed.compressed_size_bytes
            chunk.sha256 = sealed.sha256
            chunk.total_parts = sealed.total_parts
            chunk.status = ChunkStatus.SEALED.value
            chunk.sealed_at = now
            chunk.lease_until = None
            chunk.lease_token = None

            model = (
                LocalScadaMinuteRow
                if claim.data_kind == DataKind.MINUTE
                else LocalScadaEventRow
            )
            result = db.execute(
                update(model)
                .where(
                    model.chunk_id == claim.chunk_id,
                    model.sync_status == SyncStatus.SYNCING.value,
                    model.lease_token == claim.lease_token,
                )
                .values(
                    sync_status=SyncStatus.CHUNKED.value,
                    lease_until=None,
                    lease_token=None,
                )
            )
            if int(getattr(result, "rowcount", 0) or 0) != chunk.row_count:
                raise RuntimeError("chunk row association changed before seal")

    def abort_creation(self, chunk_id: UUID, reason: str) -> bool:
        with self.database.session() as db, db.begin():
            chunk = db.execute(
                select(TransferChunkRow)
                .where(TransferChunkRow.chunk_id == chunk_id)
                .with_for_update()
            ).scalar_one_or_none()
            if chunk is None or chunk.status != ChunkStatus.CREATING.value:
                return False
            require_chunk_transition(chunk.status, ChunkStatus.FAILED)
            for model in (LocalScadaMinuteRow, LocalScadaEventRow):
                db.execute(
                    update(model)
                    .where(model.chunk_id == chunk_id)
                    .values(
                        chunk_id=None,
                        sync_status=SyncStatus.PENDING.value,
                        lease_until=None,
                        lease_token=None,
                        next_retry_at=None,
                        last_sync_error=_error(reason),
                    )
                )
            chunk.status = ChunkStatus.FAILED.value
            chunk.lease_until = None
            chunk.lease_token = None
            chunk.last_error = _error(reason)
            return True

    def expired_creating_ids(self, now: datetime) -> list[UUID]:
        with self.database.session() as db:
            return list(
                db.scalars(
                    select(TransferChunkRow.chunk_id).where(
                        TransferChunkRow.status == ChunkStatus.CREATING.value,
                        TransferChunkRow.lease_until < _as_utc(now),
                    )
                )
            )

    @staticmethod
    def _descriptor(row: TransferChunkRow) -> ChunkDescriptor:
        if not row.file_path or not row.manifest_path or not row.sha256:
            raise RuntimeError(f"sealed chunk {row.chunk_id} has incomplete metadata")
        return ChunkDescriptor(
            chunk_id=row.chunk_id,
            transfer_id=row.transfer_id,
            sequence_number=row.sequence_number,
            data_kind=DataKind(row.data_kind),
            source_node_id=row.source_node_id,
            lagoon_id=row.lagoon_id,
            file_path=Path(row.file_path),
            manifest_path=Path(row.manifest_path),
            from_timestamp=_as_utc(row.from_timestamp),
            to_timestamp=_as_utc(row.to_timestamp),
            row_count=row.row_count,
            uncompressed_size_bytes=row.uncompressed_size_bytes,
            compressed_size_bytes=row.compressed_size_bytes,
            sha256=row.sha256,
            part_size_bytes=row.part_size_bytes,
            total_parts=row.total_parts,
            confirmed_parts=tuple(sorted(int(value) for value in row.confirmed_parts)),
            status=ChunkStatus(row.status),
            retry_count=row.retry_count,
            lease_token=row.lease_token,
        )

    def recover_upload_leases(self, now: datetime) -> int:
        with self.database.session() as db, db.begin():
            result = db.execute(
                update(TransferChunkRow)
                .where(
                    TransferChunkRow.status == ChunkStatus.UPLOADING.value,
                    TransferChunkRow.lease_until < _as_utc(now),
                )
                .values(
                    status=ChunkStatus.PENDING.value,
                    lease_until=None,
                    lease_token=None,
                    last_error="recovered expired upload lease",
                )
            )
            return int(getattr(result, "rowcount", 0) or 0)

    def claim_for_transfer(
        self, now: datetime, config: TransferConfig
    ) -> ChunkDescriptor | None:
        now = _as_utc(now)
        with self.database.session() as db, db.begin():
            sealed = list(
                db.scalars(
                    select(TransferChunkRow)
                    .where(TransferChunkRow.status == ChunkStatus.SEALED.value)
                    .order_by(TransferChunkRow.sequence_number)
                    .with_for_update(skip_locked=True)
                )
            )
            for row in sealed:
                require_chunk_transition(row.status, ChunkStatus.PENDING)
                row.status = ChunkStatus.PENDING.value

            candidate = db.execute(
                select(TransferChunkRow)
                .where(
                    TransferChunkRow.status.in_(
                        [
                            ChunkStatus.PENDING.value,
                            ChunkStatus.UPLOADED.value,
                            ChunkStatus.IMPORTING.value,
                        ]
                    ),
                    or_(
                        TransferChunkRow.next_retry_at.is_(None),
                        TransferChunkRow.next_retry_at <= now,
                    ),
                    TransferChunkRow.lease_token.is_(None),
                )
                .order_by(TransferChunkRow.sequence_number)
                .limit(1)
                .with_for_update(skip_locked=True)
            ).scalar_one_or_none()
            if candidate is None:
                return None

            token = uuid4()
            if candidate.status == ChunkStatus.PENDING.value:
                require_chunk_transition(candidate.status, ChunkStatus.UPLOADING)
                candidate.status = ChunkStatus.UPLOADING.value
            candidate.retry_count += 1
            candidate.lease_token = token
            candidate.lease_until = now + timedelta(seconds=config.lease_seconds)
            candidate.next_retry_at = None
            candidate.last_error = None
            db.flush()
            return self._descriptor(candidate)

    @staticmethod
    def _owned_chunk(db: Session, descriptor: ChunkDescriptor) -> TransferChunkRow:
        row = db.execute(
            select(TransferChunkRow)
            .where(TransferChunkRow.chunk_id == descriptor.chunk_id)
            .with_for_update()
        ).scalar_one()
        if row.lease_token != descriptor.lease_token:
            raise RuntimeError("chunk transfer lease was lost")
        return row

    def confirm_part(self, descriptor: ChunkDescriptor, part_number: int) -> None:
        if not 1 <= part_number <= descriptor.total_parts:
            raise ValueError("part number is outside the chunk")
        with self.database.session() as db, db.begin():
            row = self._owned_chunk(db, descriptor)
            confirmed = {int(value) for value in row.confirmed_parts}
            confirmed.add(part_number)
            row.confirmed_parts = sorted(confirmed)

    def apply_remote_status(
        self,
        descriptor: ChunkDescriptor,
        remote_status: str,
        now: datetime,
        config: TransferConfig,
        *,
        confirmed_parts: list[int] | None = None,
        staged_rows: int | None = None,
        inserted_rows: int | None = None,
        duplicate_rows: int | None = None,
        last_error: str | None = None,
    ) -> ChunkStatus:
        now = _as_utc(now)
        normalized = remote_status.upper()
        mapping = {
            "RECEIVED": ChunkStatus.UPLOADED,
            "UPLOADED": ChunkStatus.UPLOADED,
            "IMPORTING": ChunkStatus.IMPORTING,
            "VERIFIED": ChunkStatus.VERIFIED,
            "FAILED": ChunkStatus.FAILED,
            "QUARANTINED": ChunkStatus.QUARANTINED,
        }
        with self.database.session() as db, db.begin():
            row = self._owned_chunk(db, descriptor)
            target = mapping.get(normalized, ChunkStatus(row.status))
            if confirmed_parts is not None:
                row.confirmed_parts = sorted({int(value) for value in confirmed_parts})
            row.staged_rows = staged_rows
            row.inserted_rows = inserted_rows
            row.duplicate_rows = duplicate_rows
            row.last_error = _error(last_error)
            if target != ChunkStatus(row.status):
                require_chunk_transition(row.status, target)
                row.status = target.value

            if target == ChunkStatus.VERIFIED:
                row.verified_at = now
                row.uploaded_at = row.uploaded_at or now
                total = 0
                for model in (LocalScadaMinuteRow, LocalScadaEventRow):
                    result = db.execute(
                        update(model)
                        .where(
                            model.chunk_id == row.chunk_id,
                            model.sync_status == SyncStatus.CHUNKED.value,
                        )
                        .values(
                            sync_status=SyncStatus.SENT.value,
                            synced_at=now,
                            lease_until=None,
                            lease_token=None,
                            last_sync_error=None,
                        )
                    )
                    total += int(getattr(result, "rowcount", 0) or 0)
                if total != row.row_count:
                    raise RuntimeError("VERIFIED chunk does not own its expected local rows")
                row.next_retry_at = None
            elif target in {ChunkStatus.UPLOADED, ChunkStatus.IMPORTING}:
                row.uploaded_at = row.uploaded_at or now
                row.next_retry_at = now + timedelta(seconds=config.poll_interval_seconds)
            else:
                row.next_retry_at = None
            if target in {
                ChunkStatus.UPLOADED,
                ChunkStatus.IMPORTING,
                ChunkStatus.VERIFIED,
            }:
                row.retry_count = 0
            row.lease_until = None
            row.lease_token = None
            return target

    def schedule_retry(
        self,
        descriptor: ChunkDescriptor,
        next_retry_at: datetime,
        error: str,
    ) -> None:
        with self.database.session() as db, db.begin():
            row = self._owned_chunk(db, descriptor)
            if row.status == ChunkStatus.UPLOADING.value:
                require_chunk_transition(row.status, ChunkStatus.PENDING)
                row.status = ChunkStatus.PENDING.value
            row.next_retry_at = _as_utc(next_retry_at)
            row.last_error = _error(error)
            row.lease_until = None
            row.lease_token = None

    def schedule_import_retry(
        self,
        descriptor: ChunkDescriptor,
        next_retry_at: datetime,
        error: str,
        now: datetime,
    ) -> None:
        with self.database.session() as db, db.begin():
            row = self._owned_chunk(db, descriptor)
            if row.status == ChunkStatus.UPLOADING.value:
                require_chunk_transition(row.status, ChunkStatus.UPLOADED)
                row.status = ChunkStatus.UPLOADED.value
                row.uploaded_at = _as_utc(now)
            row.next_retry_at = _as_utc(next_retry_at)
            row.last_error = _error(error)
            row.lease_until = None
            row.lease_token = None

    def fail_permanently(
        self,
        descriptor: ChunkDescriptor,
        error: str,
        *,
        quarantine: bool,
    ) -> ChunkStatus:
        target = ChunkStatus.QUARANTINED if quarantine else ChunkStatus.FAILED
        with self.database.session() as db, db.begin():
            row = self._owned_chunk(db, descriptor)
            require_chunk_transition(row.status, target)
            row.status = target.value
            row.last_error = _error(error)
            row.next_retry_at = None
            row.lease_until = None
            row.lease_token = None
            return target

    def update_paths(self, chunk_id: UUID, file_path: Path, manifest_path: Path) -> None:
        with self.database.session() as db, db.begin():
            row = db.get(TransferChunkRow, chunk_id)
            if row is None:
                raise RuntimeError("chunk does not exist")
            row.file_path = str(file_path)
            row.manifest_path = str(manifest_path)

    def chunk_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in ChunkStatus}
        with self.database.session() as db:
            for status, count in db.execute(
                select(TransferChunkRow.status, func.count()).group_by(TransferChunkRow.status)
            ):
                counts[str(status)] = int(count)
        return counts

    def retry_failed(self) -> int:
        with self.database.session() as db, db.begin():
            rows = list(
                db.scalars(
                    select(TransferChunkRow)
                    .where(
                        TransferChunkRow.status == ChunkStatus.FAILED.value,
                        TransferChunkRow.file_path.is_not(None),
                        TransferChunkRow.manifest_path.is_not(None),
                        TransferChunkRow.sha256.is_not(None),
                        TransferChunkRow.total_parts > 0,
                    )
                    .with_for_update(skip_locked=True)
                )
            )
            for row in rows:
                require_chunk_transition(row.status, ChunkStatus.PENDING)
                row.status = ChunkStatus.PENDING.value
                row.retry_count = 0
                row.next_retry_at = None
                row.last_error = None
                row.lease_until = None
                row.lease_token = None
            return len(rows)

    def oldest_pending_timestamp(self) -> datetime | None:
        with self.database.session() as db:
            minute = db.scalar(
                select(func.min(LocalScadaMinuteRow.bucket)).where(
                    LocalScadaMinuteRow.sync_status.in_(
                        [
                            SyncStatus.PENDING.value,
                            SyncStatus.SYNCING.value,
                            SyncStatus.CHUNKED.value,
                        ]
                    )
                )
            )
            event = db.scalar(
                select(func.min(LocalScadaEventRow.started_at)).where(
                    LocalScadaEventRow.sync_status.in_(
                        [
                            SyncStatus.PENDING.value,
                            SyncStatus.SYNCING.value,
                            SyncStatus.CHUNKED.value,
                        ]
                    )
                )
            )
        values = [_as_utc(value) for value in (minute, event) if value is not None]
        return min(values) if values else None

    def verified_files_before(self, cutoff: datetime) -> list[ChunkDescriptor]:
        with self.database.session() as db:
            rows = list(
                db.scalars(
                    select(TransferChunkRow)
                    .where(
                        TransferChunkRow.status == ChunkStatus.VERIFIED.value,
                        TransferChunkRow.file_deleted_at.is_(None),
                        TransferChunkRow.verified_at < _as_utc(cutoff),
                    )
                    .order_by(TransferChunkRow.verified_at)
                )
            )
            return [self._descriptor(row) for row in rows]

    def mark_file_deleted(self, chunk_id: UUID, now: datetime) -> None:
        with self.database.session() as db, db.begin():
            row = db.get(TransferChunkRow, chunk_id)
            if row is None or row.status != ChunkStatus.VERIFIED.value:
                raise RuntimeError("only VERIFIED chunk files can be deleted")
            row.file_deleted_at = _as_utc(now)

    def cleanup_verified_metadata(
        self, now: datetime, config: RetentionConfig
    ) -> int:
        cutoff = _as_utc(now) - timedelta(days=config.verified_metadata_days)
        with self.database.session() as db, db.begin():
            ids = list(
                db.scalars(
                    select(TransferChunkRow.chunk_id)
                    .where(
                        TransferChunkRow.status == ChunkStatus.VERIFIED.value,
                        TransferChunkRow.file_deleted_at.is_not(None),
                        TransferChunkRow.verified_at < cutoff,
                    )
                    .order_by(TransferChunkRow.verified_at)
                    .limit(config.batch_size)
                )
            )
            if not ids:
                return 0
            for model in (LocalScadaMinuteRow, LocalScadaEventRow):
                db.execute(
                    update(model).where(model.chunk_id.in_(ids)).values(chunk_id=None)
                )
            result = db.execute(
                delete(TransferChunkRow).where(TransferChunkRow.chunk_id.in_(ids))
            )
            return int(getattr(result, "rowcount", 0) or 0)
