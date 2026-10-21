"""Minimal reminder storage boundary; delivery is outside this contract."""

from typing import Protocol
from uuid import UUID

from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery
from app.planner.reminder import Reminder


class ReminderStorageError(Exception):
    """Reminder storage is unavailable or contains invalid data."""


class ReminderAlreadyExistsError(ReminderStorageError):
    """An existing reminder ID cannot be overwritten by add."""


class ReminderRepository(Protocol):
    async def add(self, reminder: Reminder, *, operation: CreationOperation | None = None) -> Reminder:
        """Store reminder data without scheduling or delivering it."""
        ...

    async def get(self, *, reminder_id: UUID, owner_id: str) -> Reminder | None:
        """Return the owner's reminder or None if missing or inaccessible."""
        ...

    async def list(self, query: RecordQuery) -> list[Reminder]:
        """List or search accessible records in stable time/ID order."""
        ...

    async def get_created(self, *, owner_id: str, operation: CreationOperation) -> Reminder | None:
        """Replay a matching creation or raise OperationConflictError."""
        ...
