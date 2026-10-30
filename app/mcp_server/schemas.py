"""Public DTOs of the MCP tools (and later the Mini App REST API); see INTEGRATION.md."""

# Module alias: DTO fields named date and datetime (public contract) would shadow the types.
import datetime as dt
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.planner.meeting import Meeting
from app.planner.query import RecordSort, local_day_start_utc
from app.planner.reminder import Reminder
from app.planner.task import Task

EventType = Literal["task", "meeting", "reminder"]
EVENT_TYPES: tuple[EventType, ...] = ("task", "meeting", "reminder")
ErrorCode = Literal[
    "validation_error", "not_found", "operation_conflict", "temporary_unavailable", "unauthorized"
]


class EventDTO(BaseModel):
    """One record as clients see it; owner_id is deliberately absent."""

    model_config = ConfigDict(frozen=True)

    id: UUID
    event_type: EventType
    name: str
    description: str
    status: Literal["todo", "in_progress", "done"] | None
    date: dt.date | None
    datetime: dt.datetime | None
    end_time: dt.datetime | None
    created_at: dt.datetime

    @classmethod
    def from_record(cls, record: Task | Meeting | Reminder) -> "EventDTO":
        if isinstance(record, Task):
            return cls(
                id=record.id,
                event_type="task",
                name=record.title,
                description=record.description,
                status=record.status,
                date=record.due_date,
                datetime=record.due_at,
                end_time=record.ends_at,
                created_at=record.created_at,
            )
        if isinstance(record, Meeting):
            return cls(
                id=record.id,
                event_type="meeting",
                name=record.title,
                description=record.description,
                status=None,
                date=None,
                datetime=record.starts_at,
                end_time=record.ends_at,
                created_at=record.created_at,
            )
        return cls(
            id=record.id,
            event_type="reminder",
            name=record.text,
            description=record.description,
            status=None,
            date=None,
            datetime=record.remind_at,
            end_time=None,
            created_at=record.created_at,
        )

    def sort_key(self, timezone: str) -> tuple[dt.datetime, UUID]:
        """Order by the record's moment; a date-only deadline sorts at its local midnight."""
        if self.datetime is not None:
            return self.datetime, self.id
        if self.date is not None:
            return local_day_start_utc(self.date, timezone), self.id
        return self.created_at, self.id


def error(code: ErrorCode, message: str, field: str | None = None) -> dict[str, Any]:
    """Structured tool error; message is safe to show and contains no internal details."""
    return {"error": {"code": code, "message": message, "field": field}}


def events_result(events: list[EventDTO], **extra: Any) -> dict[str, Any]:
    return {"events": [event.model_dump(mode="json") for event in events], **extra}


def sort_events(events: list[EventDTO], sort: RecordSort, timezone: str) -> list[EventDTO]:
    """Merge separately ordered type pages with the same rules as PostgreSQL (NULLS LAST)."""
    ordered = sorted(events, key=lambda event: event.id)
    reverse = sort.endswith("_desc")
    if sort.startswith("name_"):
        return sorted(ordered, key=lambda event: event.name.lower().replace("ё", "е"), reverse=reverse)
    if sort.startswith("created_"):
        return sorted(ordered, key=lambda event: event.created_at, reverse=reverse)
    if sort.startswith("duration_"):
        durations = [
            (event.end_time - event.datetime, event)
            for event in ordered
            if event.event_type == "meeting" and event.end_time is not None and event.datetime is not None
        ]
        known = {event.id for _, event in durations}
        return [event for _, event in sorted(durations, key=lambda pair: pair[0], reverse=reverse)] + [
            event for event in ordered if event.id not in known
        ]
    moments: list[tuple[dt.datetime, EventDTO]] = []
    missing: list[EventDTO] = []
    for event in ordered:
        moment = event.datetime
        if moment is None and event.date is not None:
            moment = local_day_start_utc(event.date, timezone)
        if moment is None:
            missing.append(event)
        else:
            moments.append((moment, event))
    return [event for _, event in sorted(moments, key=lambda pair: pair[0], reverse=reverse)] + missing
