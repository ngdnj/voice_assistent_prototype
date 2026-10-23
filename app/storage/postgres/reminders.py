"""Reminders in the PostgreSQL events table; implements app.planner.reminder_repository.ReminderRepository."""

from typing import Any
from uuid import UUID

from app.planner.reminder import Reminder
from app.planner.reminder_repository import ReminderAlreadyExistsError, ReminderStorageError
from app.storage.postgres.records import PostgresEventStore, RecordRow, require


class PostgresReminderRepository(PostgresEventStore[Reminder]):
    event_type = "reminder"
    operation_type = "create_reminder"
    storage_error = ReminderStorageError
    already_exists_error = ReminderAlreadyExistsError
    period_column = "scheduled_at"

    def _to_row(self, record: Reminder) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_id": record.owner_id,
            "event_type": self.event_type,
            "name": record.text,
            "description": record.description,
            "status": None,
            "due_date": None,
            "scheduled_at": record.remind_at,
            "ends_at": None,
            "created_at": record.created_at,
            "updated_at": record.created_at,
        }

    def _from_row(self, row: RecordRow) -> Reminder:
        return Reminder(
            id=row["id"],
            owner_id=row["owner_id"],
            text=row["name"],
            description=row["description"],
            remind_at=require(row, "scheduled_at"),
            created_at=row["created_at"],
        )

    async def get(self, *, reminder_id: UUID, owner_id: str) -> Reminder | None:
        return await self._get(reminder_id, owner_id)
