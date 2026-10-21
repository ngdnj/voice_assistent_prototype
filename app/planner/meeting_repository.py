"""Minimal storage boundary for meetings."""

from typing import Protocol
from uuid import UUID

from app.planner.meeting import Meeting


class MeetingStorageError(Exception):
    """Meeting storage is unavailable or contains invalid data."""


class MeetingAlreadyExistsError(MeetingStorageError):
    """An existing meeting ID cannot be overwritten by add."""


class MeetingRepository(Protocol):
    async def add(self, meeting: Meeting) -> None:
        """Store a meeting before returning successfully."""
        ...

    async def get(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        """Return the owner's meeting, or None if missing or inaccessible."""
        ...
