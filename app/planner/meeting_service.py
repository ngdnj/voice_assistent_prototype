"""Meeting operations with explicitly supplied storage and time."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.planner.meeting import Meeting, create_meeting
from app.planner.meeting_repository import MeetingRepository


class MeetingService:
    def __init__(
        self, *, repository: MeetingRepository, clock: Callable[[], datetime]
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def create_meeting(
        self, *, owner_id: str, title: str, starts_at: datetime, ends_at: datetime
    ) -> Meeting:
        meeting = create_meeting(
            owner_id=owner_id, title=title, starts_at=starts_at,
            ends_at=ends_at, created_at=self._clock(),
        )
        await self._repository.add(meeting)
        return meeting

    async def get_meeting(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        return await self._repository.get(meeting_id=meeting_id, owner_id=owner_id)
