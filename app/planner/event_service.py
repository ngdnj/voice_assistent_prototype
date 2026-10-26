"""Rename, reschedule and delete owned records without transport or SQL dependencies."""

from collections.abc import Callable
from dataclasses import replace
from datetime import date, datetime
from uuid import UUID

from app.planner.event_repository import EventMutationRepository, EventRecord
from app.planner.fields import check_name_length
from app.planner.meeting import Meeting, move_meeting
from app.planner.operation import CreationOperation
from app.planner.reminder import PastReminderTimeError, Reminder
from app.planner.task import Task, normalize_due
from app.planner.time import to_utc


class EventService:
    def __init__(self, *, repository: EventMutationRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    async def update_event(
        self,
        *,
        event_id: UUID,
        owner_id: str,
        operation_id: str,
        name: str | None = None,
        moment: datetime | None = None,
        day: date | None = None,
        end_time: datetime | None = None,
    ) -> EventRecord | None:
        if all(value is None for value in (name, moment, day, end_time)):
            raise ValueError("No changes provided")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("title must not be blank")
            name = name.strip()
            check_name_length(name, "title")
        day, moment = normalize_due(day, moment)
        end_time = None if end_time is None else to_utc(end_time)
        data = {"event_id": str(event_id)}
        for field, value in (("name", name), ("datetime", moment), ("date", day), ("end_time", end_time)):
            if value is not None:
                data[field] = value if isinstance(value, str) else value.isoformat()
        operation = CreationOperation.from_data(operation_id, **data)
        replay = await self._repository.replay(owner_id=owner_id, operation=operation, kind="update_event")
        if replay is not None:
            return replay
        previous = await self._repository.get_owned(event_id=event_id, owner_id=owner_id)
        if previous is None:
            return await self._repository.replay(owner_id=owner_id, operation=operation, kind="update_event")
        now = to_utc(self._clock())
        if not isinstance(previous, Task) and day is not None:
            raise ValueError("task_date is only supported for tasks")
        if not isinstance(previous, Meeting) and end_time is not None:
            raise ValueError("meeting_end is only supported for meetings")
        record: EventRecord
        if isinstance(previous, Meeting):
            record = move_meeting(previous, starts_at=moment, ends_at=end_time)
            record = replace(record, title=name if name is not None else record.title)
        elif isinstance(previous, Reminder):
            if moment is not None and moment < now:
                raise PastReminderTimeError("remind_at must not precede the update time")
            record = replace(
                previous,
                text=name if name is not None else previous.text,
                remind_at=moment if moment is not None else previous.remind_at,
            )
        else:
            change_due = day is not None or moment is not None
            record = replace(
                previous,
                title=name if name is not None else previous.title,
                due_date=day if change_due else previous.due_date,
                due_at=moment if change_due else previous.due_at,
            )
        return await self._repository.update(record, previous=previous, operation=operation, changed_at=now)

    async def delete_event(self, *, event_id: UUID, owner_id: str, operation_id: str) -> EventRecord | None:
        operation = CreationOperation.from_data(operation_id, event_id=str(event_id))
        replay = await self._repository.replay(owner_id=owner_id, operation=operation, kind="delete_event")
        if replay is not None:
            return replay
        record = await self._repository.get_owned(event_id=event_id, owner_id=owner_id)
        if record is None:
            return await self._repository.replay(owner_id=owner_id, operation=operation, kind="delete_event")
        return await self._repository.delete(record, operation=operation, changed_at=to_utc(self._clock()))
