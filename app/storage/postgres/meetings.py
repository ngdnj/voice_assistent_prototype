"""Meetings in the PostgreSQL events table; implements app.planner.meeting_repository.MeetingRepository."""

from typing import Any
from uuid import UUID

from app.planner.meeting import Meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError, MeetingStorageError
from app.storage.postgres.records import PostgresEventStore, RecordRow, require


class PostgresMeetingRepository(PostgresEventStore[Meeting]):
    event_type = "meeting"
    operation_type = "create_meeting"
    storage_error = MeetingStorageError
    already_exists_error = MeetingAlreadyExistsError
    # Meetings that start in the period, as in the Redis adapter.
    period_column = "scheduled_at"

    def _to_row(self, record: Meeting) -> dict[str, Any]:
        return {
            "id": record.id,
            "owner_id": record.owner_id,
            "event_type": self.event_type,
            "name": record.title,
            "description": record.description,
            "status": None,
            "due_date": None,
            "scheduled_at": record.starts_at,
            "ends_at": record.ends_at,
            "created_at": record.created_at,
            "updated_at": record.created_at,
        }

    def _from_row(self, row: RecordRow) -> Meeting:
        return Meeting(
            id=row["id"],
            owner_id=row["owner_id"],
            title=row["name"],
            description=row["description"],
            starts_at=require(row, "scheduled_at"),
            ends_at=row["ends_at"],
            created_at=row["created_at"],
        )

    async def get(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        return await self._get(meeting_id, owner_id)
