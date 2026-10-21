"""Reminder creation and retrieval with explicit storage and clock."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.planner.query import RecordQuery
from app.planner.reminder import Reminder, create_reminder
from app.planner.reminder_repository import ReminderRepository


class ReminderService:
    def __init__(
        self, *, repository: ReminderRepository, clock: Callable[[], datetime]
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def create_reminder(
        self, *, owner_id: str, text: str, remind_at: datetime
    ) -> Reminder:
        reminder = create_reminder(
            owner_id=owner_id, text=text, remind_at=remind_at, created_at=self._clock()
        )
        await self._repository.add(reminder)
        return reminder

    async def get_reminder(self, *, reminder_id: UUID, owner_id: str) -> Reminder | None:
        return await self._repository.get(reminder_id=reminder_id, owner_id=owner_id)

    async def list_reminders(self, query: RecordQuery) -> list[Reminder]:
        return await self._repository.list(query)
