"""Meeting operations with explicitly supplied storage and time."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.planner.fields import description_fingerprint_data
from app.planner.meeting import Meeting, create_meeting, move_meeting
from app.planner.meeting_repository import MeetingRepository
from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery
from app.planner.time import to_utc


class MeetingService:
    def __init__(self, *, repository: MeetingRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    async def create_meeting(
        self,
        *,
        owner_id: str,
        title: str,
        starts_at: datetime,
        operation_id: str,
        ends_at: datetime | None = None,
        description: str = "",
    ) -> Meeting:
        starts_at = to_utc(starts_at)
        ends_at = None if ends_at is None else to_utc(ends_at)
        # A missing end is omitted; operations stored before it became optional always had one.
        end: dict[str, str] = {} if ends_at is None else {"ends_at": ends_at.isoformat()}
        operation = CreationOperation.from_data(
            operation_id,
            title=title.strip(),
            starts_at=starts_at.isoformat(),
            **end,
            **description_fingerprint_data(description),
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
            description=description,
        )
        return await self._repository.add(meeting, operation=operation)

    async def get_meeting(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        return await self._repository.get(meeting_id=meeting_id, owner_id=owner_id)

    async def reschedule_meeting(
        self,
        *,
        meeting_id: UUID,
        owner_id: str,
        operation_id: str,
        starts_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> Meeting | None:
        """Move an owned meeting; an omitted end preserves its known duration."""
        if starts_at is None and ends_at is None:
            raise ValueError("starts_at or ends_at is required")
        start = None if starts_at is None else to_utc(starts_at)
        end = None if ends_at is None else to_utc(ends_at)
        data = {"meeting_id": str(meeting_id)}
        if start is not None:
            data["starts_at"] = start.isoformat()
        if end is not None:
            data["ends_at"] = end.isoformat()
        operation = CreationOperation.from_data(operation_id, **data)
        replay = await self._repository.get_rescheduled(owner_id=owner_id, operation=operation)
        if replay is not None:
            return replay
        previous = await self._repository.get(meeting_id=meeting_id, owner_id=owner_id)
        if previous is None:
            return None
        meeting = move_meeting(previous, starts_at=start, ends_at=end)
        return await self._repository.reschedule(
            meeting,
            previous=previous,
            operation=operation,
            changed_at=to_utc(self._clock()),
        )

    async def list_meetings(self, query: RecordQuery) -> list[Meeting]:
        return await self._repository.list(query)
