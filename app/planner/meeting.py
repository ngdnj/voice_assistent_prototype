"""Meeting rules independent of transport and storage."""

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID, uuid4

from app.planner.time import to_utc


@dataclass(frozen=True, kw_only=True)
class Meeting:
    owner_id: str
    title: str
    starts_at: datetime
    ends_at: datetime
    created_at: datetime
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id must not be blank")
        title = self.title.strip()
        if not title:
            raise ValueError("title must not be blank")
        start, end, created = (
            to_utc(self.starts_at), to_utc(self.ends_at), to_utc(self.created_at)
        )
        if end <= start:
            raise ValueError("ends_at must be strictly after starts_at")
        object.__setattr__(self, "title", title)
        object.__setattr__(self, "starts_at", start)
        object.__setattr__(self, "ends_at", end)
        object.__setattr__(self, "created_at", created)


def create_meeting(
    *, owner_id: str, title: str, starts_at: datetime,
    ends_at: datetime, created_at: datetime,
) -> Meeting:
    """Create a meeting with explicitly supplied start, end and creation time."""
    return Meeting(
        owner_id=owner_id, title=title, starts_at=starts_at,
        ends_at=ends_at, created_at=created_at,
    )
