"""Shared query rules, without coupling record models or storage engines."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.planner.time import to_utc

TaskPeriod = Literal["created_at", "due"]


def local_day_start_utc(day: date, timezone: str) -> datetime:
    """First instant of a local calendar day, also where a DST change skips or repeats midnight.

    Unlike day_bounds_utc this never rejects a day: fold=0 maps a skipped midnight to the
    transition instant and a repeated midnight to its first occurrence.
    """
    return datetime.combine(day, time.min, tzinfo=ZoneInfo(timezone)).astimezone(UTC)


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
    # Tasks only: "created_at" filters creation time; "due" filters the deadline and skips tasks
    # without one. A date-only deadline belongs to the period when its local midnight does.
    task_period: TaskPeriod = "created_at"
    timezone: str | None = None

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
        if self.task_period not in ("created_at", "due"):
            raise ValueError("task_period must be created_at or due")
        if self.timezone is not None:
            try:
                ZoneInfo(self.timezone)
            except (ZoneInfoNotFoundError, ValueError):
                raise ValueError("timezone must be a known IANA time zone") from None
        if self.task_period == "due" and self.timezone is None:
            raise ValueError("timezone is required to compare date-only deadlines with a period")
        if self.query is not None:
            query = self.query.strip().casefold()
            if not query or len(query) > 200:
                raise ValueError("query must contain 1 to 200 characters")
            object.__setattr__(self, "query", query)

    def due_date_bounds(self) -> tuple[date | None, date | None]:
        """Half-open [first, last) of dates whose local midnight lies in [start_time, end_time)."""
        if self.timezone is None:
            raise ValueError("timezone is required for date-only deadlines")
        zone = self.timezone

        def first_date_from(moment: datetime | None) -> date | None:
            if moment is None:
                return None
            day = moment.astimezone(ZoneInfo(zone)).date()
            return day if local_day_start_utc(day, zone) >= moment else day + timedelta(days=1)

        return first_date_from(self.start_time), first_date_from(self.end_time)

    def includes(self, moment: datetime) -> bool:
        instant = to_utc(moment)
        return ((self.start_time is None or self.start_time <= instant)
                and (self.end_time is None or instant < self.end_time))
