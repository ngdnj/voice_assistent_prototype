"""Minimal task model independent of transports and storage."""

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4


@dataclass(frozen=True, kw_only=True)
class Task:
    owner_id: str
    title: str
    created_at: datetime
    id: UUID = field(default_factory=uuid4)
    status: Literal["active"] = field(default="active", init=False)

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id must not be blank")
        title = self.title.strip()
        if not title:
            raise ValueError("title must not be blank")
        if self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "created_at", self.created_at.astimezone(UTC))


def create_task(*, owner_id: str, title: str, created_at: datetime) -> Task:
    """Create a task in memory using explicit time and a trusted owner ID."""
    return Task(owner_id=owner_id, title=title, created_at=created_at)
