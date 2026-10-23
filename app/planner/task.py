"""Minimal task model independent of transports and storage."""

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal
from uuid import UUID, uuid4

from app.planner.fields import normalize_description
from app.planner.time import to_utc

TaskStatus = Literal["todo", "in_progress", "done"]
TASK_STATUSES: tuple[TaskStatus, ...] = ("todo", "in_progress", "done")


def normalize_due(
    due_date: date | None, due_at: datetime | None
) -> tuple[date | None, datetime | None]:
    """A task may have no deadline, a date-only deadline or an exact moment, never both."""
    if due_date is not None and due_at is not None:
        raise ValueError("due_date and due_at are mutually exclusive")
    if due_date is not None and (not isinstance(due_date, date) or isinstance(due_date, datetime)):
        raise TypeError("due_date must be a date without time")
    return due_date, None if due_at is None else to_utc(due_at)


@dataclass(frozen=True, kw_only=True)
class Task:
    owner_id: str
    title: str
    created_at: datetime
    description: str = ""
    # Date-only deadline stays a date: no midnight is substituted for a missing time.
    due_date: date | None = None
    due_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    # Restoring from storage may pass any allowed status; new tasks start as "todo".
    status: TaskStatus = "todo"

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id must not be blank")
        if self.status not in TASK_STATUSES:
            raise ValueError("status must be one of: todo, in_progress, done")
        title = self.title.strip()
        if not title:
            raise ValueError("title must not be blank")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "description", normalize_description(self.description))
        object.__setattr__(self, "created_at", to_utc(self.created_at))
        object.__setattr__(self, "due_at", normalize_due(self.due_date, self.due_at)[1])


def create_task(
    *,
    owner_id: str,
    title: str,
    created_at: datetime,
    description: str = "",
    due_date: date | None = None,
    due_at: datetime | None = None,
) -> Task:
    """Create a task in memory using explicit time and a trusted owner ID."""
    return Task(
        owner_id=owner_id,
        title=title,
        created_at=created_at,
        description=description,
        due_date=due_date,
        due_at=due_at,
    )
