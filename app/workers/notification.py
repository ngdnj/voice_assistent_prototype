"""Version 1 notification envelope; delivery state is separate from its data."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from app.planner.time import to_utc


@dataclass(frozen=True, kw_only=True)
class Notification:
    notification_id: UUID
    owner_id: str
    text: str
    created_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.notification_id, UUID):
            raise ValueError("notification_id must be UUID")
        if not self.owner_id.strip():
            raise ValueError("owner_id must not be blank")
        if not self.text.strip() or len(self.text) > 4000:
            raise ValueError("text must contain 1 to 4000 characters")
        object.__setattr__(self, "created_at", to_utc(self.created_at))

    def to_fields(self) -> dict[str, str]:
        return {
            "version": "1",
            "notification_id": str(self.notification_id),
            "owner_id": self.owner_id,
            "text": self.text,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_fields(cls, fields: dict[str, str]) -> "Notification":
        if set(fields) != {"version", "notification_id", "owner_id", "text", "created_at"}:
            raise ValueError("Invalid notification fields")
        if fields["version"] != "1":
            raise ValueError("Unsupported notification version")
        return cls(
            notification_id=UUID(fields["notification_id"]),
            owner_id=fields["owner_id"],
            text=fields["text"],
            created_at=datetime.fromisoformat(fields["created_at"]),
        )
