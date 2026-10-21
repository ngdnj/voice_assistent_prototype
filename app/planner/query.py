"""Shared query rules, without coupling record models or storage engines."""

from dataclasses import dataclass
from datetime import datetime

from app.planner.time import to_utc


MAX_SEARCH_CANDIDATES = 1000


class QueryTooBroadError(ValueError):
    """Text search needs a narrower period to stay within its work budget."""


@dataclass(frozen=True, kw_only=True)
class RecordQuery:
    owner_id: str
    start_time: datetime | None = None
    end_time: datetime | None = None
    query: str | None = None
    limit: int = 50
    offset: int = 0

    def __post_init__(self) -> None:
        if not self.owner_id.strip():
            raise ValueError("owner_id must not be blank")
        if type(self.limit) is not int or not 1 <= self.limit <= 100:
            raise ValueError("limit must be an integer between 1 and 100")
        if type(self.offset) is not int or not 0 <= self.offset <= 10000:
            raise ValueError("offset must be an integer between 0 and 10000")
        for name in ("start_time", "end_time"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, to_utc(value))
        if self.start_time is not None and self.end_time is not None:
            if self.start_time >= self.end_time:
                raise ValueError("start_time must precede end_time")
        if self.query is not None:
            query = self.query.strip().casefold()
            if not query or len(query) > 200:
                raise ValueError("query must contain 1 to 200 characters")
            object.__setattr__(self, "query", query)

    def includes(self, moment: datetime) -> bool:
        instant = to_utc(moment)
        return ((self.start_time is None or self.start_time <= instant)
                and (self.end_time is None or instant < self.end_time))
