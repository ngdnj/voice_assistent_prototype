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
        clear_schedule: bool = False,
        clear_end_time: bool = False,
    ) -> EventRecord | None:
        if not isinstance(clear_schedule, bool) or not isinstance(clear_end_time, bool):
            raise ValueError("clear_schedule and clear_end_time must be boolean")
        if clear_schedule and (any(value is not None for value in (moment, day, end_time)) or clear_end_time):
            raise ValueError("clear_schedule cannot be combined with date or time changes")
        if clear_end_time and end_time is not None:
            raise ValueError("clear_end_time cannot be combined with end_time")
        if all(value is None for value in (name, moment, day, end_time)) and not (clear_schedule or clear_end_time):
            raise ValueError("No changes provided")
        if name is not None:
            if not isinstance(name, str) or not name.strip():
                raise ValueError("title must not be blank")
            name = name.strip()
            check_name_length(name, "title")
        day, moment = normalize_due(day, moment)
        end_time = None if end_time is None else to_utc(end_time)
        data: dict[str, str] = {"event_id": str(event_id)}
        if clear_schedule:
            data["clear_schedule"] = "true"
        if clear_end_time:
            data["clear_end_time"] = "true"
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
        if clear_schedule and not isinstance(previous, Task):
            raise ValueError("clear_schedule is only supported for tasks")
        if clear_end_time and not isinstance(previous, Task | Meeting):
            raise ValueError("record_end is only supported for tasks and meetings")
        if not isinstance(previous, Task) and day is not None:
            raise ValueError("task_date is only supported for tasks")
        if not isinstance(previous, Task | Meeting) and end_time is not None:
            raise ValueError("record_end is only supported for tasks and meetings")
        record: EventRecord
        if isinstance(previous, Meeting):
            record = move_meeting(previous, starts_at=moment, ends_at=end_time)
            if clear_end_time:
                record = replace(record, ends_at=None)
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
            change_due = clear_schedule or day is not None or moment is not None
            task_end = previous.ends_at
            if clear_schedule or clear_end_time:
                task_end = None
            elif day is not None:
                if end_time is not None:
                    raise ValueError("ends_at requires due_at, not a date-only deadline")
                task_end = None
            elif end_time is not None:
                task_end = end_time
            elif moment is not None and previous.ends_at is not None and previous.due_at is not None:
                task_end = moment + (previous.ends_at - previous.due_at)
            record = replace(
                previous,
                title=name if name is not None else previous.title,
                due_date=day if change_due else previous.due_date,
                due_at=moment if change_due else previous.due_at,
                ends_at=task_end,
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
