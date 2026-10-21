"""Meeting operations with explicitly supplied storage and time."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.planner.meeting import Meeting, create_meeting
from app.planner.meeting_repository import MeetingRepository
from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery
from app.planner.time import to_utc


class MeetingService:
    def __init__(self, *, repository: MeetingRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    async def create_meeting(
        self, *, owner_id: str, title: str, starts_at: datetime, ends_at: datetime, operation_id: str
    ) -> Meeting:
        starts_at, ends_at = to_utc(starts_at), to_utc(ends_at)
        operation = CreationOperation.from_data(
            operation_id, title=title.strip(), starts_at=starts_at.isoformat(), ends_at=ends_at.isoformat()
        )
        previous = await self._repository.get_created(owner_id=owner_id, operation=operation)
        if previous is not None:
            return previous
        meeting = create_meeting(
            owner_id=owner_id,
            title=title,
            starts_at=starts_at,
            ends_at=ends_at,
            created_at=self._clock(),
        )
        return await self._repository.add(meeting, operation=operation)

    async def get_meeting(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        return await self._repository.get(meeting_id=meeting_id, owner_id=owner_id)

    async def list_meetings(self, query: RecordQuery) -> list[Meeting]:
        return await self._repository.list(query)
