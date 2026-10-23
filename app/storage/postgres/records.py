"""Shared PostgreSQL storage for tasks, meetings and reminders in the events table."""

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Any
from uuid import UUID

from sqlalchemy import ColumnElement, Row, and_, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.query import RecordQuery
from app.storage.postgres.tables import events, operations

# Column name -> value of one events row (sqlalchemy.engine.RowMapping).
type RecordRow = Mapping[Any, Any]


class PostgresEventStore[R]:
    """Owner-scoped add/get/list/replay for one event type; models are mapped by the subclass."""

    event_type: str
    operation_type: str
    storage_error: type[Exception]
    already_exists_error: type[Exception]
    # Period filter and ordering column: created_at for tasks (RecordQuery contract), scheduled_at otherwise.
    period_column: str

    def __init__(self, sessions: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = sessions

    def _to_row(self, record: R) -> dict[str, Any]:
        raise NotImplementedError

    def _from_row(self, row: RecordRow) -> R:
        raise NotImplementedError

    @staticmethod
    def _record_id(record: R) -> UUID:
        record_id: UUID = record.id  # type: ignore[attr-defined]
        return record_id

    @asynccontextmanager
    async def _storage_errors(self, message: str) -> AsyncIterator[None]:
        """Driver and network errors become the planner's storage error without driver details."""
        try:
            yield
        except (SQLAlchemyError, OSError, TimeoutError):
            raise self.storage_error(message) from None

    def _decode(self, row: Row[Any]) -> R:
        try:
            return self._from_row(row._mapping)
        except (ValueError, TypeError):
            raise self.storage_error("Stored record data is invalid") from None

    def _owned(self, owner_id: str) -> ColumnElement[bool]:
        return and_(events.c.owner_id == owner_id, events.c.event_type == self.event_type)

    async def add(self, record: R, *, operation: CreationOperation | None = None) -> R:
        """Insert the record and, if given, its operation result in one transaction."""
        row = self._to_row(record)
        replay = False
        # The session rolls back on any exit without commit, including the already-exists error.
        async with self._storage_errors("Record storage write failed"), self._sessions() as session:
            inserted = await session.scalar(
                insert(events)
                .values(**row)
                .on_conflict_do_nothing(index_elements=["id"])
                .returning(events.c.id)
            )
            if inserted is None:
                raise self.already_exists_error("Record ID already exists")
            if operation is not None:
                committed = await session.scalar(
                    insert(operations)
                    .values(
                        owner_id=row["owner_id"],
                        operation_type=self.operation_type,
                        operation_id=operation.operation_id,
                        fingerprint=operation.fingerprint,
                        event_id=inserted,
                        created_at=row["created_at"],
                    )
                    .on_conflict_do_nothing(index_elements=["owner_id", "operation_id"])
                    .returning(operations.c.event_id)
                )
                if committed is None:
                    # A concurrent request committed the same operation first: undo our record, replay theirs.
                    await session.rollback()
                    replay = True
            if not replay:
                await session.commit()
        if replay and operation is not None:
            previous = await self.get_created(owner_id=row["owner_id"], operation=operation)
            if previous is None:
                raise self.storage_error("Operation result is missing")
            return previous
        return record

    async def get_created(self, *, owner_id: str, operation: CreationOperation) -> R | None:
        """Return the record created by this operation in its current state, or raise on different data."""
        async with self._storage_errors("Operation read failed"), self._sessions() as session:
            saved = (
                await session.execute(
                    select(
                        operations.c.operation_type, operations.c.fingerprint, operations.c.event_id
                    ).where(
                        operations.c.owner_id == owner_id, operations.c.operation_id == operation.operation_id
                    )
                )
            ).first()
            # One action_id is one logical action: reusing it for another record type is a conflict too.
            matches = saved is not None and (saved.operation_type, saved.fingerprint) == (
                self.operation_type,
                operation.fingerprint,
            )
            if saved is None or not matches:
                row = None
            else:
                row = (
                    await session.execute(
                        select(events).where(events.c.id == saved.event_id, self._owned(owner_id))
                    )
                ).first()
        if saved is None:
            return None
        if not matches:
            raise OperationConflictError("operation_id was already used with different data")
        if row is None:
            raise self.storage_error("Operation result is inconsistent with its record")
        return self._decode(row)

    async def _get(self, record_id: UUID, owner_id: str) -> R | None:
        async with self._storage_errors("Record storage read failed"), self._sessions() as session:
            row = (
                await session.execute(select(events).where(events.c.id == record_id, self._owned(owner_id)))
            ).first()
        return None if row is None else self._decode(row)

    def _period(self, query: RecordQuery) -> tuple[ColumnElement[Any], list[ColumnElement[bool]]]:
        """Ordering moment and period conditions; tasks override it for deadlines."""
        moment = events.c[self.period_column]
        conditions: list[ColumnElement[bool]] = []
        if query.start_time is not None:
            conditions.append(moment >= query.start_time)
        if query.end_time is not None:
            conditions.append(moment < query.end_time)
        return moment, conditions

    async def list(self, query: RecordQuery) -> list[R]:
        """Owner's records in [start_time, end_time), optional Russian FTS, ordered by (time, id)."""
        moment, conditions = self._period(query)
        statement = select(events).where(self._owned(query.owner_id), *conditions)
        if query.query is not None:
            statement = statement.where(
                events.c.search.op("@@")(func.websearch_to_tsquery("russian", query.query))
            )
        statement = statement.order_by(moment, events.c.id).limit(query.limit).offset(query.offset)
        async with self._storage_errors("Record storage read failed"), self._sessions() as session:
            rows = (await session.execute(statement)).all()
        return [self._decode(row) for row in rows]


def require(row: RecordRow, column: str) -> Any:
    """A column the CHECK constraints guarantee for this type; a NULL means corrupted data."""
    value = row[column]
    if value is None:
        raise ValueError(f"{column} must not be NULL")
    return value
