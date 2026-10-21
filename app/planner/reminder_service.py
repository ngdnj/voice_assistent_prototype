"""Reminder creation and retrieval with explicit storage and clock."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery
from app.planner.reminder import Reminder, create_reminder
from app.planner.reminder_repository import ReminderRepository
from app.planner.time import to_utc


class ReminderService:
    def __init__(self, *, repository: ReminderRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    async def create_reminder(
        self, *, owner_id: str, text: str, remind_at: datetime, operation_id: str
    ) -> Reminder:
        remind_at = to_utc(remind_at)
        operation = CreationOperation.from_data(
            operation_id, text=text.strip(), remind_at=remind_at.isoformat()
        )
        previous = await self._repository.get_created(owner_id=owner_id, operation=operation)
        if previous is not None:
            return previous
        reminder = create_reminder(
            owner_id=owner_id, text=text, remind_at=remind_at, created_at=self._clock()
        )
        return await self._repository.add(reminder, operation=operation)

    async def get_reminder(self, *, reminder_id: UUID, owner_id: str) -> Reminder | None:
        return await self._repository.get(reminder_id=reminder_id, owner_id=owner_id)

    async def list_reminders(self, query: RecordQuery) -> list[Reminder]:
        return await self._repository.list(query)
