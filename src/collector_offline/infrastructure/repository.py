from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from collector_offline.config import RetentionConfig, SupervisorConfig
from collector_offline.domain.models import (
    HealthResult,
    NodeState,
    SessionStatus,
    StateSnapshot,
    SyncStatus,
    TagObservation,
    TransitionResult,
)
from collector_offline.infrastructure.database import Database
from collector_offline.infrastructure.models import (
    EdgeTagStateRow,
    LocalScadaEventRow,
    LocalScadaMinuteRow,
    OutageSessionRow,
    RuntimeStateRow,
)


def _truncated_error(value: str | None) -> str | None:
    return value[:2000] if value else None


def _iso_utc(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()


class EdgeRepository:
    def __init__(self, database: Database, source_node_id: str, lagoon_id: str) -> None:
        self.database = database
        self.source_node_id = source_node_id
        self.lagoon_id = lagoon_id

    def initialize_runtime(self) -> StateSnapshot:
        """Create/reconcile the singleton after a crash or power loss."""
        with self.database.session() as db, db.begin():
            row = db.get(RuntimeStateRow, 1)
            active = (
                db.execute(
                    select(OutageSessionRow)
                    .where(OutageSessionRow.status.in_(["offline", "syncing"]))
                    .order_by(OutageSessionRow.started_at.desc())
                    .with_for_update()
                )
                .scalars()
                .first()
            )

            if row is None:
                state = active.status if active else NodeState.ONLINE.value
                row = RuntimeStateRow(
                    id=1,
                    state=state,
                    current_session_id=active.id if active else None,
                )
                db.add(row)
                db.flush()
            elif active:
                row.state = active.status
                row.current_session_id = active.id
            elif row.state != NodeState.ONLINE.value:
                # A torn transition without an active session is safest as ONLINE:
                # local capture can only run with an explicit outage session.
                row.state = NodeState.ONLINE.value
                row.current_session_id = None
                row.failure_count = 0
                row.recovery_count = 0
                row.last_error = "recovered inconsistent runtime state without active outage"

            return self._snapshot(row)

    def snapshot(self) -> StateSnapshot:
        with self.database.session() as db:
            row = db.get(RuntimeStateRow, 1)
            if row is None:
                raise RuntimeError("runtime state has not been initialized")
            return self._snapshot(row)

    @staticmethod
    def _snapshot(row: RuntimeStateRow) -> StateSnapshot:
        return StateSnapshot(
            state=NodeState(row.state),
            failure_count=row.failure_count,
            recovery_count=row.recovery_count,
            current_session_id=row.current_session_id,
            last_health_at=row.last_health_at,
            last_health_ok=row.last_health_ok,
            last_error=row.last_error,
        )

    def process_health(
        self, result: HealthResult, thresholds: SupervisorConfig
    ) -> TransitionResult:
        with self.database.session() as db, db.begin():
            row = db.execute(
                select(RuntimeStateRow).where(RuntimeStateRow.id == 1).with_for_update()
            ).scalar_one()
            before = NodeState(row.state)
            reason = result.reason or (
                "central_ready" if result.available else "central_unavailable"
            )
            row.last_health_at = result.checked_at
            row.last_health_ok = result.available
            if not result.available:
                row.last_error = _truncated_error(reason)
            elif before != NodeState.SYNCING:
                row.last_error = None

            if result.available:
                row.failure_count = 0
                if before == NodeState.OFFLINE:
                    row.recovery_count += 1
                    if row.recovery_count >= thresholds.recovery_threshold:
                        self._enter_syncing(db, row, result.checked_at)
                elif before == NodeState.ONLINE:
                    row.recovery_count = 0
            else:
                row.recovery_count = 0
                row.failure_count += 1
                if before == NodeState.ONLINE and row.failure_count >= thresholds.failure_threshold:
                    self._enter_offline(db, row, result.checked_at, reason)
                elif (
                    before == NodeState.SYNCING
                    and row.failure_count >= thresholds.syncing_failure_threshold
                ):
                    self._return_to_offline(db, row)

            after = NodeState(row.state)
            return TransitionResult(
                before=before,
                after=after,
                reason=reason,
                session_id=row.current_session_id,
            )

    def _enter_offline(
        self, db: Session, row: RuntimeStateRow, started_at: datetime, reason: str
    ) -> None:
        # A new outage starts with a fresh baseline. State retained from an older
        # outage must not be interpreted as a change that happened offline.
        db.execute(delete(EdgeTagStateRow).where(EdgeTagStateRow.lagoon_id == self.lagoon_id))
        session = OutageSessionRow(
            source_node_id=self.source_node_id,
            lagoon_id=self.lagoon_id,
            started_at=started_at,
            status=SessionStatus.OFFLINE.value,
            failure_reason=_truncated_error(reason),
        )
        db.add(session)
        db.flush()
        row.state = NodeState.OFFLINE.value
        row.current_session_id = session.id
        row.failure_count = 0
        row.recovery_count = 0

    def _enter_syncing(
        self, db: Session, row: RuntimeStateRow, restored_at: datetime
    ) -> None:
        if row.current_session_id is None:
            raise RuntimeError("cannot enter syncing without an outage session")
        session = db.get(OutageSessionRow, row.current_session_id)
        if session is None:
            raise RuntimeError("current outage session does not exist")

        # Ownership boundary: the local collector stops first. Any still-open
        # local event is closed at restoration; realtime owns later transitions.
        open_events = db.execute(
            select(LocalScadaEventRow).where(
                LocalScadaEventRow.outage_session_id == session.id,
                LocalScadaEventRow.ended_at.is_(None),
            )
        ).scalars()
        for event in open_events:
            event.ended_at = restored_at
            event.sync_status = SyncStatus.PENDING.value
            event.sync_attempts = 0
            event.next_retry_at = None
            event.lease_until = None
            event.lease_token = None
            state_row = db.get(EdgeTagStateRow, (event.lagoon_id, event.tag_id))
            if state_row and state_row.open_event_id == event.id:
                state_row.open_event_id = None

        session.connectivity_restored_at = restored_at
        session.status = SessionStatus.SYNCING.value
        row.state = NodeState.SYNCING.value
        row.failure_count = 0
        row.recovery_count = 0

    @staticmethod
    def _return_to_offline(db: Session, row: RuntimeStateRow) -> None:
        if row.current_session_id is None:
            raise RuntimeError("cannot return offline without an outage session")
        session = db.get(OutageSessionRow, row.current_session_id)
        if session is None:
            raise RuntimeError("current outage session does not exist")
        session.status = SessionStatus.OFFLINE.value
        session.connectivity_restored_at = None
        row.state = NodeState.OFFLINE.value
        row.failure_count = 0
        row.recovery_count = 0

    def complete_sync_if_drained(self, completed_at: datetime) -> bool:
        with self.database.session() as db, db.begin():
            row = db.execute(
                select(RuntimeStateRow).where(RuntimeStateRow.id == 1).with_for_update()
            ).scalar_one()
            if row.state != NodeState.SYNCING.value or row.current_session_id is None:
                return False
            session_id = row.current_session_id
            outstanding = self._session_outstanding_count(db, session_id)
            if outstanding:
                return False
            session = db.get(OutageSessionRow, session_id)
            if session is None:
                raise RuntimeError("current outage session does not exist")
            session.status = SessionStatus.COMPLETED.value
            session.sync_completed_at = completed_at
            row.state = NodeState.ONLINE.value
            row.current_session_id = None
            row.failure_count = 0
            row.recovery_count = 0
            row.last_sync_at = completed_at
            row.last_error = None
            return True

    @staticmethod
    def _session_outstanding_count(db: Session, session_id: UUID) -> int:
        minute_count = db.scalar(
            select(func.count())
            .select_from(LocalScadaMinuteRow)
            .where(
                LocalScadaMinuteRow.outage_session_id == session_id,
                LocalScadaMinuteRow.sync_status != SyncStatus.SENT.value,
            )
        )
        event_count = db.scalar(
            select(func.count())
            .select_from(LocalScadaEventRow)
            .where(
                LocalScadaEventRow.outage_session_id == session_id,
                LocalScadaEventRow.sync_status != SyncStatus.SENT.value,
            )
        )
        return int(minute_count or 0) + int(event_count or 0)

    def persist_capture(
        self,
        observations: Iterable[TagObservation],
        discrete_values: dict[str, tuple[int, str | None]],
    ) -> tuple[int, int]:
        minute_count = 0
        event_count = 0
        observations_list = list(observations)
        if not observations_list:
            return (0, 0)

        with self.database.session() as db, db.begin():
            runtime = db.execute(
                select(RuntimeStateRow).where(RuntimeStateRow.id == 1).with_for_update()
            ).scalar_one()
            if runtime.state != NodeState.OFFLINE.value or runtime.current_session_id is None:
                return (0, 0)
            session_id = runtime.current_session_id

            for observation in observations_list:
                existing = db.scalar(
                    select(LocalScadaMinuteRow.id).where(
                        LocalScadaMinuteRow.lagoon_id == observation.lagoon_id,
                        LocalScadaMinuteRow.tag_id == observation.tag_id,
                        LocalScadaMinuteRow.bucket == observation.bucket,
                    )
                )
                if existing is None:
                    db.add(
                        LocalScadaMinuteRow(
                            outage_session_id=session_id,
                            lagoon_id=observation.lagoon_id,
                            tag_id=observation.tag_id,
                            bucket=observation.bucket,
                            state=observation.value.state,
                            value_num=observation.value.value_num,
                            value_bool=observation.value.value_bool,
                            captured_at=observation.captured_at,
                        )
                    )
                    minute_count += 1

                discrete = discrete_values.get(observation.tag_id)
                if discrete is None:
                    continue
                next_state, label = discrete
                state_row = db.get(EdgeTagStateRow, (observation.lagoon_id, observation.tag_id))
                if state_row is None:
                    db.add(
                        EdgeTagStateRow(
                            lagoon_id=observation.lagoon_id,
                            tag_id=observation.tag_id,
                            last_value=next_state,
                        )
                    )
                    continue

                if state_row.last_value == next_state:
                    continue

                if state_row.open_event_id:
                    open_event = db.get(LocalScadaEventRow, state_row.open_event_id)
                    if open_event and open_event.ended_at is None:
                        open_event.ended_at = observation.captured_at
                        open_event.sync_status = SyncStatus.PENDING.value
                        open_event.sync_attempts = 0
                        open_event.next_retry_at = None
                        open_event.lease_until = None
                        open_event.lease_token = None

                event = LocalScadaEventRow(
                    outage_session_id=session_id,
                    lagoon_id=observation.lagoon_id,
                    tag_id=observation.tag_id,
                    tag_label=label or observation.tag_id,
                    previous_state=state_row.last_value,
                    state=next_state,
                    started_at=observation.captured_at,
                )
                db.add(event)
                db.flush()
                state_row.last_value = next_state
                state_row.open_event_id = event.id
                event_count += 1

        return (minute_count, event_count)

    def backlog_counts(self) -> dict[str, int]:
        counts = {status.value: 0 for status in SyncStatus}
        with self.database.session() as db:
            for model in (LocalScadaMinuteRow, LocalScadaEventRow):
                rows = db.execute(
                    select(model.sync_status, func.count()).group_by(model.sync_status)
                ).all()
                for status, count in rows:
                    counts[str(status)] = counts.get(str(status), 0) + int(count)
        return counts

    def retry_failed(self) -> int:
        total = 0
        with self.database.session() as db, db.begin():
            for model in (LocalScadaMinuteRow, LocalScadaEventRow):
                result = db.execute(
                    update(model)
                    .where(model.sync_status == SyncStatus.FAILED.value)
                    .values(
                        sync_status=SyncStatus.PENDING.value,
                        sync_attempts=0,
                        next_retry_at=None,
                        lease_until=None,
                        lease_token=None,
                    )
                )
                total += int(getattr(result, "rowcount", 0) or 0)
        return total

    def cleanup(self, now: datetime, config: RetentionConfig) -> dict[str, int]:
        cutoff = now - timedelta(days=config.days)
        deleted = {"minutes": 0, "events": 0, "sessions": 0}
        with self.database.session() as db, db.begin():
            minute_ids = list(
                db.scalars(
                    select(LocalScadaMinuteRow.id)
                    .where(
                        LocalScadaMinuteRow.sync_status == SyncStatus.SENT.value,
                        LocalScadaMinuteRow.synced_at < cutoff,
                    )
                    .order_by(LocalScadaMinuteRow.synced_at)
                    .limit(config.batch_size)
                )
            )
            if minute_ids:
                result = db.execute(
                    delete(LocalScadaMinuteRow).where(LocalScadaMinuteRow.id.in_(minute_ids))
                )
                deleted["minutes"] = int(getattr(result, "rowcount", 0) or 0)

            event_ids = list(
                db.scalars(
                    select(LocalScadaEventRow.id)
                    .where(
                        LocalScadaEventRow.sync_status == SyncStatus.SENT.value,
                        LocalScadaEventRow.ended_at.is_not(None),
                        LocalScadaEventRow.synced_at < cutoff,
                    )
                    .order_by(LocalScadaEventRow.synced_at)
                    .limit(config.batch_size)
                )
            )
            if event_ids:
                result = db.execute(
                    delete(LocalScadaEventRow).where(LocalScadaEventRow.id.in_(event_ids))
                )
                deleted["events"] = int(getattr(result, "rowcount", 0) or 0)

            session_ids = list(
                db.scalars(
                    select(OutageSessionRow.id)
                    .where(
                        OutageSessionRow.status == SessionStatus.COMPLETED.value,
                        OutageSessionRow.sync_completed_at < cutoff,
                        ~select(LocalScadaMinuteRow.id)
                        .where(LocalScadaMinuteRow.outage_session_id == OutageSessionRow.id)
                        .exists(),
                        ~select(LocalScadaEventRow.id)
                        .where(LocalScadaEventRow.outage_session_id == OutageSessionRow.id)
                        .exists(),
                    )
                    .order_by(OutageSessionRow.sync_completed_at)
                    .limit(config.batch_size)
                )
            )
            if session_ids:
                result = db.execute(
                    delete(OutageSessionRow).where(OutageSessionRow.id.in_(session_ids))
                )
                deleted["sessions"] = int(getattr(result, "rowcount", 0) or 0)
        return deleted

    def operational_status(self) -> dict[str, Any]:
        snapshot = self.snapshot()
        counts = self.backlog_counts()
        outage_started_at = None
        last_sync_at = None
        with self.database.session() as db:
            if snapshot.current_session_id:
                session = db.get(OutageSessionRow, snapshot.current_session_id)
                outage_started_at = _iso_utc(session.started_at) if session else None
            runtime = db.get(RuntimeStateRow, 1)
            if runtime and runtime.last_sync_at:
                last_sync_at = _iso_utc(runtime.last_sync_at)
        return {
            "state": snapshot.state.value,
            "source_node_id": self.source_node_id,
            "lagoon_id": self.lagoon_id,
            "current_session_id": (
                str(snapshot.current_session_id) if snapshot.current_session_id else None
            ),
            "outage_started_at": outage_started_at,
            "last_health_at": (
                _iso_utc(snapshot.last_health_at) if snapshot.last_health_at else None
            ),
            "last_health_ok": snapshot.last_health_ok,
            "backlog": counts,
            "last_sync_at": last_sync_at,
            "last_error": snapshot.last_error,
        }
