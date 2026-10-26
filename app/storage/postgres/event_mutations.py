"""Atomic edit/delete with a persistent action identity and an owner filter."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import Row, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.planner.event_repository import EventMutationStorageError, EventRecord, MutationKind
from app.planner.meeting import Meeting
from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.task import Task
from app.storage.postgres.meetings import PostgresMeetingRepository
from app.storage.postgres.reminders import PostgresReminderRepository
from app.storage.postgres.tables import events, operations
from app.storage.postgres.tasks import PostgresTaskRepository


class PostgresEventMutations:
    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions
        self._readers: dict[
            str, PostgresTaskRepository | PostgresMeetingRepository | PostgresReminderRepository
        ] = {
            "task": PostgresTaskRepository(sessions),
            "meeting": PostgresMeetingRepository(sessions),
            "reminder": PostgresReminderRepository(sessions),
        }

    @asynccontextmanager
    async def _errors(self) -> AsyncIterator[None]:
        try:
            yield
        except (SQLAlchemyError, OSError, TimeoutError):
            raise EventMutationStorageError("Record editing storage is unavailable") from None

    def _decode(self, row: Row[Any]) -> EventRecord:
        reader = self._readers.get(row.event_type)
        if reader is None:
            raise EventMutationStorageError("Invalid stored event type")
        # Reuse the concrete row-to-model mappings and their domain validation.
        try:
            return reader._from_row(row._mapping)
        except (ValueError, TypeError, KeyError):
            raise EventMutationStorageError("Invalid stored event") from None

    async def get_owned(self, *, event_id: UUID, owner_id: str) -> EventRecord | None:
        async with self._errors(), self._sessions() as session:
            row = (
                await session.execute(
                    select(events).where(
                        events.c.id == event_id,
                        events.c.owner_id == owner_id,
                        events.c.deleted_at.is_(None),
                    )
                )
            ).first()
        return None if row is None else self._decode(row)

    @staticmethod
    async def _saved(
        session: AsyncSession, owner_id: str, operation: CreationOperation, kind: MutationKind
    ) -> UUID | None:
        saved = (
            await session.execute(
                select(operations).where(
                    operations.c.owner_id == owner_id,
                    operations.c.operation_id == operation.operation_id,
                )
            )
        ).first()
        if saved is None:
            return None
        if (saved.operation_type, saved.fingerprint) != (kind, operation.fingerprint):
            raise OperationConflictError("operation_id was already used with different data")
        event_id = saved.event_id
        if not isinstance(event_id, UUID):
            raise EventMutationStorageError("Invalid operation record identity")
        return event_id

    def _replayed(self, row: Row[Any], kind: MutationKind) -> EventRecord:
        if row.deleted_at is not None and kind != "delete_event":
            raise OperationConflictError("The record was deleted; replay cannot restore it")
        if kind == "delete_event" and row.deleted_at is None:
            raise EventMutationStorageError("Deletion result is inconsistent")
        return self._decode(row)

    async def replay(
        self, *, owner_id: str, operation: CreationOperation, kind: MutationKind
    ) -> EventRecord | None:
        async with self._errors(), self._sessions() as session:
            event_id = await self._saved(session, owner_id, operation, kind)
            if event_id is None:
                return None
            row = (
                await session.execute(
                    select(events).where(
                        events.c.id == event_id,
                        events.c.owner_id == owner_id,
                    )
                )
            ).first()
            if row is None:
                raise EventMutationStorageError("Operation record is missing")
            return self._replayed(row, kind)

    async def update(
        self,
        record: EventRecord,
        *,
        previous: EventRecord,
        operation: CreationOperation,
        changed_at: datetime,
    ) -> EventRecord | None:
        return await self._commit(
            record, previous=previous, operation=operation, changed_at=changed_at, kind="update_event"
        )

    async def delete(
        self,
        record: EventRecord,
        *,
        operation: CreationOperation,
        changed_at: datetime,
    ) -> EventRecord | None:
        return await self._commit(
            record, previous=record, operation=operation, changed_at=changed_at, kind="delete_event"
        )

    async def _commit(
        self,
        record: EventRecord,
        *,
        previous: EventRecord,
        operation: CreationOperation,
        changed_at: datetime,
        kind: MutationKind,
    ) -> EventRecord | None:
        async with self._errors(), self._sessions() as session:
            row = (
                await session.execute(
                    select(events)
                    .where(
                        events.c.id == record.id,
                        events.c.owner_id == record.owner_id,
                    )
                    .with_for_update()
                )
            ).first()
            if row is None:
                return None
            saved_id = await self._saved(session, record.owner_id, operation, kind)
            if saved_id is not None:
                if saved_id != record.id:
                    raise OperationConflictError("Operation record mismatch")
                return self._replayed(row, kind)
            if row.deleted_at is not None:
                return None
            if self._decode(row) != previous:
                raise OperationConflictError("Record changed concurrently; read it again")
            inserted = await session.scalar(
                insert(operations)
                .values(
                    owner_id=record.owner_id,
                    operation_id=operation.operation_id,
                    operation_type=kind,
                    fingerprint=operation.fingerprint,
                    event_id=record.id,
                    created_at=changed_at,
                )
                .on_conflict_do_nothing(index_elements=["owner_id", "operation_id"])
                .returning(operations.c.event_id)
            )
            if inserted is None:
                raise OperationConflictError("operation_id was already used with different data")
            values: dict[str, Any]
            if kind == "delete_event":
                values = {"deleted_at": changed_at, "updated_at": changed_at}
            else:
                if isinstance(record, Task):
                    values = {
                        "name": record.title,
                        "due_date": record.due_date,
                        "scheduled_at": record.due_at,
                        "ends_at": None,
                    }
                elif isinstance(record, Meeting):
                    values = {
                        "name": record.title,
                        "due_date": None,
                        "scheduled_at": record.starts_at,
                        "ends_at": record.ends_at,
                    }
                else:
                    values = {
                        "name": record.text,
                        "due_date": None,
                        "scheduled_at": record.remind_at,
                        "ends_at": None,
                    }
                values["updated_at"] = changed_at
            await session.execute(
                update(events)
                .where(
                    events.c.id == record.id,
                    events.c.owner_id == record.owner_id,
                )
                .values(**values)
            )
            await session.commit()
            return record
