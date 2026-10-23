"""Reminder data and validation, independent of delivery."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.planner.fields import normalize_description
from app.planner.time import to_utc


class PastReminderTimeError(ValueError):
    """A new reminder cannot be scheduled before its creation time."""


@dataclass(frozen=True, kw_only=True)
class Reminder:
    owner_id: str
    text: str
    remind_at: datetime
    created_at: datetime
    description: str = ""
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id must not be blank")
        text = self.text.strip()
        if not text:
            raise ValueError("text must not be blank")
        remind_at, created_at = to_utc(self.remind_at), to_utc(self.created_at)
        if remind_at < created_at:
            raise PastReminderTimeError("remind_at must not precede created_at")
        object.__setattr__(self, "text", text)
        object.__setattr__(self, "description", normalize_description(self.description))
        object.__setattr__(self, "remind_at", remind_at)
        object.__setattr__(self, "created_at", created_at)


def create_reminder(
    *, owner_id: str, text: str, remind_at: datetime, created_at: datetime, description: str = ""
) -> Reminder:
    return Reminder(
        owner_id=owner_id, text=text, remind_at=remind_at, created_at=created_at,
        description=description,
    )
