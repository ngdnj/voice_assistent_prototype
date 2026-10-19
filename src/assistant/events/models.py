"""Domain model of user records: tasks, reminders and meetings."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class EventType(StrEnum):
    TASK = "task"
    REMINDER = "reminder"
    MEETING = "meeting"


class EventStatus(StrEnum):
    PENDING = "pending"
    DONE = "done"


# Types that make no sense without a concrete moment in time.
TYPES_REQUIRING_DATETIME = frozenset({EventType.REMINDER, EventType.MEETING})


@dataclass(frozen=True, slots=True)
class Event:
    id: int
    user_id: str
    event_type: EventType
    name: str
    description: str
    when: datetime | None
    created_at: datetime
    status: EventStatus = EventStatus.PENDING

    def to_dict(self) -> dict[str, str | int | None]:
        return {
            "id": self.id,
            "event_type": self.event_type.value,
            "name": self.name,
            "description": self.description,
            "datetime": self.when.isoformat(timespec="seconds") if self.when else None,
            "created_at": self.created_at.isoformat(timespec="seconds"),
            "status": self.status.value,
        }
