"""Tasks in the PostgreSQL events table; implements app.planner.repository.TaskRepository."""

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, and_, func, literal, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.types import TIMESTAMP

from app.planner.query import RecordQuery
from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.task import Task, TaskStatus
from app.storage.postgres.records import PostgresEventStore, RecordRow, require
from app.storage.postgres.tables import events


class PostgresTaskRepository(PostgresEventStore[Task]):
    event_type = "task"
    operation_type = "create_task"
    storage_error = TaskStorageError
    already_exists_error = TaskAlreadyExistsError
    # RecordQuery contract for tasks: the period filters creation time, not the deadline.
    period_column = "created_at"

    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        super().__init__(sessions)
        self._clock = clock

    def _to_row(self, record: Task) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_id": record.owner_id,
            "event_type": self.event_type,
            "name": record.title,
            "description": record.description,
            "status": record.status,
            "due_date": record.due_date,
            "scheduled_at": record.due_at,
            "ends_at": record.ends_at,
            "created_at": record.created_at,
            "updated_at": record.created_at,
        }

    def _from_row(self, row: RecordRow) -> Task:
        return Task(
            id=row["id"],
            owner_id=row["owner_id"],
            title=row["name"],
            description=row["description"],
            status=require(row, "status"),
            due_date=row["due_date"],
            due_at=row["scheduled_at"],
            ends_at=row["ends_at"],
            created_at=row["created_at"],
        )

    def _period(self, query: RecordQuery) -> tuple[ColumnElement[Any], list[ColumnElement[bool]]]:
        if query.task_period == "created_at":
            return super()._period(query)
        # Deadline period: an exact due_at in [start, end) or a due_date whose local midnight is there.
        first_date, last_date = query.due_date_bounds()
        exact: list[ColumnElement[bool]] = [events.c.scheduled_at.is_not(None)]
        dated: list[ColumnElement[bool]] = [events.c.due_date.is_not(None)]
        if query.start_time is not None:
            exact.append(
                or_(
                    events.c.ends_at > query.start_time,
                    and_(events.c.ends_at.is_(None), events.c.scheduled_at >= query.start_time),
                )
            )
            dated.append(events.c.due_date >= first_date)
        if query.end_time is not None:
            exact.append(events.c.scheduled_at < query.end_time)
            dated.append(events.c.due_date < last_date)
        local_midnight = func.timezone(literal(query.timezone), func.cast(events.c.due_date, TIMESTAMP))
        moment = func.coalesce(events.c.scheduled_at, local_midnight)
        return moment, [or_(and_(*exact), and_(*dated))]

    async def get(self, *, task_id: UUID, owner_id: str) -> Task | None:
        return await self._get(task_id, owner_id)

    async def update_status(self, *, task_id: UUID, owner_id: str, status: TaskStatus) -> Task | None:
        """One UPDATE with the owner filter; a repeated status changes nothing, not even updated_at."""
        async with self._storage_errors("Task status update failed"), self._sessions() as session:
            row = (
                await session.execute(
                    update(events)
                    .where(events.c.id == task_id, self._owned(owner_id), events.c.status != status)
                    .values(status=status, updated_at=self._clock())
                    .returning(*events.c)
                )
            ).first()
            await session.commit()
            if row is None:
                row = (
                    await session.execute(select(events).where(events.c.id == task_id, self._owned(owner_id)))
                ).first()
        return None if row is None else self._decode(row)
