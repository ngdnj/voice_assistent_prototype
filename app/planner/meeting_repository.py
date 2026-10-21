"""Minimal storage boundary for meetings."""

from typing import Protocol
from uuid import UUID

from app.planner.meeting import Meeting
from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery


class MeetingStorageError(Exception):
    """Meeting storage is unavailable or contains invalid data."""


class MeetingAlreadyExistsError(MeetingStorageError):
    """An existing meeting ID cannot be overwritten by add."""


class MeetingRepository(Protocol):
    async def add(self, meeting: Meeting, *, operation: CreationOperation | None = None) -> Meeting:
        """Store a meeting before returning successfully."""
        ...

    async def get(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        """Return the owner's meeting, or None if missing or inaccessible."""
        ...

    async def list(self, query: RecordQuery) -> list[Meeting]:
        """List or search accessible records in stable time/ID order."""
        ...

    async def get_created(self, *, owner_id: str, operation: CreationOperation) -> Meeting | None:
        """Replay a matching creation or raise OperationConflictError."""
        ...
