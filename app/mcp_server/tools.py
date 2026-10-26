"""Transport-independent MCP logic: parse arguments, call planner, shape results.

No business rules live here: names, dates and statuses are validated by app/planner. This layer
only checks argument shapes, maps planner errors to the structured error format and limits output.
"""

import logging
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

from app.mcp_server.context import TrustedContext
from app.mcp_server.schemas import EVENT_TYPES, EventDTO, EventType, error, events_result, sort_events
from app.planner.event_repository import EventMutationStorageError
from app.planner.event_service import EventService
from app.planner.meeting_repository import MeetingStorageError
from app.planner.meeting_service import MeetingService
from app.planner.operation import OperationConflictError
from app.planner.query import RECORD_SORTS, RecordQuery, RecordSort
from app.planner.reminder import PastReminderTimeError
from app.planner.reminder_repository import ReminderStorageError
from app.planner.reminder_service import ReminderService
from app.planner.repository import TaskStorageError
from app.planner.service import TaskService
from app.planner.task import TASK_STATUSES
from app.planner.time import AmbiguousLocalTimeError, NonexistentLocalTimeError

LOGGER = logging.getLogger("it_gorky.mcp")

MAX_PERIOD = timedelta(days=366)
PERIOD_LIMIT = 100
SEARCH_LIMIT_DEFAULT = 10
SEARCH_LIMIT_MAX = 50
STORAGE_ERRORS = (TaskStorageError, MeetingStorageError, ReminderStorageError, EventMutationStorageError)
DATE_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")


class ArgumentError(ValueError):
    def __init__(self, message: str, field: str | None) -> None:
        super().__init__(message)
        self.message = message
        self.field = field


def _sort(value: Any) -> RecordSort:
    if value is None:
        return "time_asc"
    if not isinstance(value, str) or value not in RECORD_SORTS:
        raise ArgumentError("Неизвестный порядок сортировки", "sort")
    return value


def _limit(value: Any, *, default: int, maximum: int) -> int:
    if value is None:
        return default
    if type(value) is not int or not 1 <= value <= maximum:
        raise ArgumentError(f"limit — целое от 1 до {maximum}", "limit")
    return value


def _moment(value: Any, field: str, *, required: bool = False) -> datetime | None:
    if value is None:
        if required:
            raise ArgumentError("Нужно точное время в ISO 8601 со смещением", field)
        return None
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        moment = None
    if moment is None or moment.utcoffset() is None:
        raise ArgumentError(
            "Время нужно в ISO 8601 с UTC или смещением, например 2026-10-24T15:00:00+03:00", field
        )
    return moment


def _date(value: Any, field: str) -> date | None:
    if value is None:
        return None
    try:
        # Strictly YYYY-MM-DD: fromisoformat would also accept ISO week dates such as 2026-W43-1.
        day = date.fromisoformat(value) if isinstance(value, str) and DATE_FORMAT.fullmatch(value) else None
    except ValueError:
        day = None
    if day is None:
        raise ArgumentError("Дата нужна в формате YYYY-MM-DD", field)
    return day


def _text(value: Any, field: str, *, required: bool) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise ArgumentError("Ожидается строка", field)
    if "\x00" in value:
        # PostgreSQL text cannot store NUL; this is a client error, not a storage outage to retry.
        raise ArgumentError("Текст содержит недопустимый символ", field)
    return value


def _types(value: Any) -> tuple[EventType, ...]:
    if value is None:
        return EVENT_TYPES
    if not isinstance(value, list) or not value or any(item not in EVENT_TYPES for item in value):
        raise ArgumentError("event_types — непустой список из task, meeting, reminder", "event_types")
    return tuple(item for item in EVENT_TYPES if item in value)


def _period(start: Any, end: Any, *, required: bool) -> tuple[datetime | None, datetime | None]:
    start_time = _moment(start, "start_time", required=required)
    end_time = _moment(end, "end_time", required=required)
    if start_time is not None and end_time is not None:
        if start_time >= end_time:
            raise ArgumentError("start_time должно быть раньше end_time", "end_time")
        if end_time - start_time > MAX_PERIOD:
            raise ArgumentError("Период не длиннее 366 дней", "end_time")
    return start_time, end_time


# Planner messages are English; the agent gets a short Russian explanation and the argument name.
_PLANNER_FIELDS = (
    ("task_date", "date", "Дата без времени допустима только у задачи"),
    ("meeting_end", "end_time", "Окончание можно менять только у встречи"),
    ("description", "description", "Описание длиннее 2000 символов"),
    ("title", "name", "Название не может быть пустым и длиннее 200 символов"),
    ("text", "name", "Название не может быть пустым и длиннее 200 символов"),
    ("ends_at", "end_time", "Окончание встречи должно быть позже начала"),
    ("due", "date", "Укажите либо date, либо datetime"),
)


def _planner_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PastReminderTimeError):
        return error("validation_error", "Время напоминания уже прошло", "datetime")
    if isinstance(exc, AmbiguousLocalTimeError | NonexistentLocalTimeError):
        return error("validation_error", "Такого местного времени нет или оно неоднозначно", "datetime")
    message = str(exc)
    for marker, field, text in _PLANNER_FIELDS:
        if marker in message:
            return error("validation_error", text, field)
    return error("validation_error", "Неверные данные записи", None)


@dataclass(frozen=True)
class EventTools:
    tasks: TaskService
    meetings: MeetingService
    reminders: ReminderService
    editor: EventService

    async def update_event(self, context: TrustedContext, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._edit(context, arguments, delete=False)

    async def delete_event(self, context: TrustedContext, arguments: dict[str, Any]) -> dict[str, Any]:
        return await self._edit(context, arguments, delete=True)

    async def _edit(
        self, context: TrustedContext, arguments: dict[str, Any], *, delete: bool
    ) -> dict[str, Any]:
        if context.action_id is None:
            return error("unauthorized", "Нет X-Action-Id: идентификатор действия задаёт backend", None)
        try:
            event_id = UUID(_text(arguments.get("event_id"), "event_id", required=True))
        except ValueError:
            return error("not_found", "Запись не найдена", "event_id")
        try:
            if delete:
                record = await self.editor.delete_event(
                    event_id=event_id, owner_id=context.owner_id, operation_id=context.action_id
                )
            else:
                name = arguments.get("name")
                record = await self.editor.update_event(
                    event_id=event_id,
                    owner_id=context.owner_id,
                    operation_id=context.action_id,
                    name=None if name is None else _text(name, "name", required=True),
                    moment=_moment(arguments.get("datetime"), "datetime"),
                    day=_date(arguments.get("date"), "date"),
                    end_time=_moment(arguments.get("end_time"), "end_time"),
                )
        except ArgumentError as exc:
            return error("validation_error", exc.message, exc.field)
        except OperationConflictError:
            return error("operation_conflict", "Действие или запись уже изменились; перечитайте запись", None)
        except STORAGE_ERRORS:
            LOGGER.warning(
                "storage_unavailable",
                extra={
                    "request_id": context.request_id,
                    "tool": "delete_event" if delete else "update_event",
                },
            )
            return error("temporary_unavailable", "Хранилище недоступно, изменение не подтверждено", None)
        except (ValueError, TypeError) as exc:
            return _planner_error(exc)
        if record is None:
            return error("not_found", "Запись не найдена или уже удалена", "event_id")
        return {"deleted" if delete else "updated": EventDTO.from_record(record).model_dump(mode="json")}

    async def reschedule_meeting(self, context: TrustedContext, arguments: dict[str, Any]) -> dict[str, Any]:
        if context.action_id is None:
            return error("unauthorized", "Нет X-Action-Id: идентификатор действия задаёт backend", None)
        try:
            meeting_id = UUID(_text(arguments.get("meeting_id"), "meeting_id", required=True))
        except (ArgumentError, ValueError):
            return error("not_found", "Встреча не найдена", "meeting_id")
        try:
            start = _moment(arguments.get("datetime"), "datetime")
            end = _moment(arguments.get("end_time"), "end_time")
            if start is None and end is None:
                raise ArgumentError("Укажите новое начало или окончание встречи", "datetime")
            record = await self.meetings.reschedule_meeting(
                meeting_id=meeting_id,
                owner_id=context.owner_id,
                operation_id=context.action_id,
                starts_at=start,
                ends_at=end,
            )
        except ArgumentError as exc:
            return error("validation_error", exc.message, exc.field)
        except OperationConflictError:
            return error(
                "operation_conflict", "Действие или встреча уже изменились; перечитайте запись", None
            )
        except STORAGE_ERRORS:
            LOGGER.warning(
                "storage_unavailable", extra={"request_id": context.request_id, "tool": "reschedule_meeting"}
            )
            return error(
                "temporary_unavailable", "Хранилище временно недоступно, перенос не подтверждён", None
            )
        except (ValueError, TypeError) as exc:
            return _planner_error(exc)
        if record is None:
            return error("not_found", "Встреча не найдена", "meeting_id")
        return {"updated": EventDTO.from_record(record).model_dump(mode="json")}

    async def create_event(self, context: TrustedContext, arguments: dict[str, Any]) -> dict[str, Any]:
        if context.action_id is None:
            return error("unauthorized", "Нет X-Action-Id: идентификатор действия задаёт backend", None)
        try:
            event_type = arguments.get("event_type")
            if event_type not in EVENT_TYPES:
                raise ArgumentError("event_type — task, meeting или reminder", "event_type")
            name = _text(arguments.get("name"), "name", required=True)
            description = _text(arguments.get("description"), "description", required=False)
            due_date = _date(arguments.get("date"), "date")
            moment = _moment(arguments.get("datetime"), "datetime", required=event_type != "task")
            end_time = _moment(arguments.get("end_time"), "end_time")
            if event_type != "task" and due_date is not None:
                raise ArgumentError("Дата без времени допустима только у задачи; укажите datetime", "date")
            if event_type != "meeting" and end_time is not None:
                raise ArgumentError("end_time допустимо только у встречи", "end_time")
            if due_date is not None and moment is not None:
                raise ArgumentError("Укажите либо date, либо datetime", "date")
        except ArgumentError as exc:
            return error("validation_error", exc.message, exc.field)
        common = {"owner_id": context.owner_id, "operation_id": context.action_id, "description": description}
        try:
            record: Any
            if event_type == "task":
                record = await self.tasks.create_task(title=name, due_date=due_date, due_at=moment, **common)
            elif event_type == "meeting":
                assert moment is not None
                record = await self.meetings.create_meeting(
                    title=name, starts_at=moment, ends_at=end_time, **common
                )
            else:
                assert moment is not None
                record = await self.reminders.create_reminder(text=name, remind_at=moment, **common)
        except OperationConflictError:
            # A ValueError subclass: must be handled before ordinary validation errors.
            return error("operation_conflict", "Это действие уже выполнено с другими данными", None)
        except STORAGE_ERRORS:
            LOGGER.warning(
                "storage_unavailable", extra={"request_id": context.request_id, "tool": "create_event"}
            )
            return error(
                "temporary_unavailable", "Хранилище временно недоступно, запись не подтверждена", None
            )
        except (ValueError, TypeError) as exc:
            return _planner_error(exc)
        return {"created": EventDTO.from_record(record).model_dump(mode="json")}

    async def _collect(
        self, context: TrustedContext, types: tuple[EventType, ...], *, sort: RecordSort, **query: Any
    ) -> list[EventDTO]:
        events: list[EventDTO] = []
        # With a period, tasks are selected by deadline; a search without a period covers all tasks.
        has_period = query.get("start_time") is not None or query.get("end_time") is not None
        for event_type in types:
            records: list[Any]
            if event_type == "task":
                records = await self.tasks.list_tasks(
                    RecordQuery(
                        owner_id=context.owner_id,
                        task_period="due" if has_period else "created_at",
                        timezone=context.timezone,
                        sort=sort,
                        **query,
                    )
                )
            elif event_type == "meeting":
                records = await self.meetings.list_meetings(
                    RecordQuery(owner_id=context.owner_id, sort=sort, **query)
                )
            else:
                records = await self.reminders.list_reminders(
                    RecordQuery(owner_id=context.owner_id, sort=sort, **query)
                )
            events.extend(EventDTO.from_record(record) for record in records)
        return sort_events(events, sort, context.timezone)

    async def get_events_by_period(
        self, context: TrustedContext, arguments: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            start_time, end_time = _period(
                arguments.get("start_time"), arguments.get("end_time"), required=True
            )
            types = _types(arguments.get("event_types"))
            sort = _sort(arguments.get("sort"))
            limit = _limit(arguments.get("limit"), default=PERIOD_LIMIT, maximum=PERIOD_LIMIT)
        except ArgumentError as exc:
            return error("validation_error", exc.message, exc.field)
        try:
            events = await self._collect(
                context,
                types,
                sort=sort,
                start_time=start_time,
                end_time=end_time,
                limit=min(limit + 1, PERIOD_LIMIT),
            )
        except STORAGE_ERRORS:
            LOGGER.warning(
                "storage_unavailable",
                extra={"request_id": context.request_id, "tool": "get_events_by_period"},
            )
            return error("temporary_unavailable", "Хранилище временно недоступно", None)
        except ValueError:
            return error("validation_error", "Период нельзя сопоставить с местными датами", "start_time")
        # A full page of any type means more records may exist beyond the limit.
        return events_result(events[:limit], truncated=len(events) > limit or len(events) >= PERIOD_LIMIT)

    async def get_event_by_query(self, context: TrustedContext, arguments: dict[str, Any]) -> dict[str, Any]:
        try:
            text = _text(arguments.get("query"), "query", required=True)
            if not text.strip() or len(text.strip()) > 200:
                raise ArgumentError("query — от 1 до 200 символов", "query")
            types = _types(arguments.get("event_types"))
            start_time, end_time = _period(
                arguments.get("start_time"), arguments.get("end_time"), required=False
            )
            limit = _limit(arguments.get("limit"), default=SEARCH_LIMIT_DEFAULT, maximum=SEARCH_LIMIT_MAX)
            sort = _sort(arguments.get("sort"))
        except ArgumentError as exc:
            return error("validation_error", exc.message, exc.field)
        try:
            events = await self._collect(
                # One extra result per type detects a shortened page after the common sort.
                context,
                types,
                sort=sort,
                query=text,
                start_time=start_time,
                end_time=end_time,
                limit=limit + 1,
            )
        except STORAGE_ERRORS:
            LOGGER.warning(
                "storage_unavailable", extra={"request_id": context.request_id, "tool": "get_event_by_query"}
            )
            return error("temporary_unavailable", "Хранилище временно недоступно", None)
        except ValueError:
            return error("validation_error", "Неверные параметры поиска", None)
        return events_result(events[:limit], **({"truncated": True} if len(events) > limit else {}))

    async def update_task_status(self, context: TrustedContext, task_id: str, status: Any) -> dict[str, Any]:
        """Mini App status switch (REST only, not an MCP tool): todo, in_progress or done."""
        try:
            record_id = UUID(task_id)
        except ValueError:
            return error("not_found", "Задача не найдена", "task_id")
        if status not in TASK_STATUSES:
            return error("validation_error", "status — todo, in_progress или done", "status")
        try:
            task = await self.tasks.update_status(task_id=record_id, owner_id=context.owner_id, status=status)
        except STORAGE_ERRORS:
            LOGGER.warning(
                "storage_unavailable", extra={"request_id": context.request_id, "tool": "update_task_status"}
            )
            return error("temporary_unavailable", "Хранилище временно недоступно, статус не изменён", None)
        if task is None:
            # Missing and other users' tasks look the same.
            return error("not_found", "Задача не найдена", "task_id")
        return {"event": EventDTO.from_record(task).model_dump(mode="json")}
