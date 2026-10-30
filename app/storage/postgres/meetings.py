"""Meetings in the PostgreSQL events table; implements app.planner.meeting_repository.MeetingRepository."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from app.planner.meeting import Meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError, MeetingStorageError
from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.query import RecordQuery
from app.storage.postgres.records import PostgresEventStore, RecordRow, require
from app.storage.postgres.tables import events, operations


class PostgresMeetingRepository(PostgresEventStore[Meeting]):
    event_type = "meeting"
    operation_type = "create_meeting"
    storage_error = MeetingStorageError
    already_exists_error = MeetingAlreadyExistsError
    # Meetings with an explicit end are visible throughout their interval.
    period_column = "scheduled_at"

    def _period(self, query: RecordQuery) -> tuple[ColumnElement[Any], list[ColumnElement[bool]]]:
        start, end = events.c.scheduled_at, events.c.ends_at
        conditions: list[ColumnElement[bool]] = []
        if query.start_time is not None:
            conditions.append(
                or_(
                    end > query.start_time,
                    and_(end.is_(None), start >= query.start_time),
                )
            )
        if query.end_time is not None:
            conditions.append(start < query.end_time)
        return start, conditions

    def _to_row(self, record: Meeting) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_id": record.owner_id,
            "event_type": self.event_type,
            "name": record.title,
            "description": record.description,
            "status": None,
            "due_date": None,
            "scheduled_at": record.starts_at,
            "ends_at": record.ends_at,
            "created_at": record.created_at,
            "updated_at": record.created_at,
        }

    def _from_row(self, row: RecordRow) -> Meeting:
        return Meeting(
            id=row["id"],
            owner_id=row["owner_id"],
            title=row["name"],
            description=row["description"],
            starts_at=require(row, "scheduled_at"),
            ends_at=row["ends_at"],
            created_at=row["created_at"],
        )

    async def get(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        return await self._get(meeting_id, owner_id)

    async def get_rescheduled(self, *, owner_id: str, operation: CreationOperation) -> Meeting | None:
        return await self._get_operation(owner_id=owner_id, operation=operation, kind="reschedule_meeting")

    async def reschedule(
        self,
        meeting: Meeting,
        *,
        previous: Meeting,
        operation: CreationOperation,
        changed_at: datetime,
    ) -> Meeting | None:
        async with self._storage_errors("Meeting reschedule failed"), self._sessions() as session:
            row = (
                await session.execute(
                    select(events)
                    .where(events.c.id == meeting.id, self._owned(meeting.owner_id))
                    .with_for_update()
                )
            ).first()
            if row is None:
                return None
            # The row lock serializes changes to this meeting. The operation PK also protects
            # against using one action for two different meetings concurrently.
            saved = (
                await session.execute(
                    select(operations).where(
                        operations.c.owner_id == meeting.owner_id,
                        operations.c.operation_id == operation.operation_id,
                    )
                )
            ).first()
            if saved is not None:
                if (saved.operation_type, saved.fingerprint) != ("reschedule_meeting", operation.fingerprint):
                    raise OperationConflictError("operation_id was already used with different data")
                return self._decode(row)
            current = self._decode(row)
            if (current.starts_at, current.ends_at) != (previous.starts_at, previous.ends_at):
                raise OperationConflictError("Meeting changed concurrently; read it again")
            committed = await session.scalar(
                insert(operations)
                .values(
                    owner_id=meeting.owner_id,
                    operation_type="reschedule_meeting",
                    operation_id=operation.operation_id,
                    fingerprint=operation.fingerprint,
                    event_id=meeting.id,
                    created_at=changed_at,
                )
                .on_conflict_do_nothing(index_elements=["owner_id", "operation_id"])
                .returning(operations.c.event_id)
            )
            if committed is None:
                raise OperationConflictError("operation_id was already used with different data")
            await session.execute(
                update(events)
                .where(
                    events.c.id == meeting.id,
                    self._owned(meeting.owner_id),
                )
                .values(scheduled_at=meeting.starts_at, ends_at=meeting.ends_at, updated_at=changed_at)
            )
            await session.commit()
            return meeting
