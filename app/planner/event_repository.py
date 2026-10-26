"""Storage boundary for editing/deleting the three existing record types."""

from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from app.planner.meeting import Meeting
from app.planner.operation import CreationOperation
from app.planner.reminder import Reminder
from app.planner.task import Task

type EventRecord = Task | Meeting | Reminder
MutationKind = Literal["update_event", "delete_event"]


class EventMutationStorageError(Exception):
    """Editing storage is unavailable or inconsistent."""


class EventMutationRepository(Protocol):
    async def get_owned(self, *, event_id: UUID, owner_id: str) -> EventRecord | None: ...

    async def replay(
        self, *, owner_id: str, operation: CreationOperation, kind: MutationKind
    ) -> EventRecord | None: ...

    async def update(
        self,
        record: EventRecord,
        *,
        previous: EventRecord,
        operation: CreationOperation,
        changed_at: datetime,
    ) -> EventRecord | None: ...

    async def delete(
        self,
        record: EventRecord,
        *,
        operation: CreationOperation,
        changed_at: datetime,
    ) -> EventRecord | None: ...
