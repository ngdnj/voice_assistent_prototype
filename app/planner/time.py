"""Explicit UTC conversion and user calendar-day boundaries."""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo


class AmbiguousLocalTimeError(ValueError):
    """A local wall time corresponds to more than one instant."""


class NonexistentLocalTimeError(ValueError):
    """A local wall time was skipped by a clock transition."""


def _resolve_local(value: datetime, zone: ZoneInfo) -> datetime:
    candidates: set[datetime] = set()
    for fold in (0, 1):
        candidate = value.replace(tzinfo=zone, fold=fold).astimezone(UTC)
        if candidate.astimezone(zone).replace(tzinfo=None) == value:
            candidates.add(candidate)
    if not candidates:
        raise NonexistentLocalTimeError("Local time does not exist in this zone")
    if len(candidates) > 1:
        raise AmbiguousLocalTimeError("Local time is ambiguous; supply UTC or a fixed offset")
    return candidates.pop()


def to_utc(value: datetime) -> datetime:
    """Require an aware instant; reject gaps and ambiguous ZoneInfo wall times."""
    if not isinstance(value, datetime):
        raise TypeError("Expected datetime, not a date without time")
    if value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    if isinstance(value.tzinfo, ZoneInfo):
        return _resolve_local(value.replace(tzinfo=None), value.tzinfo)
    return value.astimezone(UTC)


def local_to_utc(value: datetime, timezone_name: str) -> datetime:
    """Resolve explicit local wall time in an IANA zone without guessing fold."""
    if not isinstance(value, datetime):
        raise TypeError("Expected local datetime")
    if value.tzinfo is not None:
        raise ValueError("Local datetime must have no tzinfo; use to_utc for an instant")
    return _resolve_local(value, ZoneInfo(timezone_name))


def day_bounds_utc(day: date, timezone_name: str) -> tuple[datetime, datetime]:
    """Return [local midnight, next local midnight) in UTC, or reject ambiguity."""
    if not isinstance(day, date) or isinstance(day, datetime):
        raise TypeError("Expected date without time")
    zone = ZoneInfo(timezone_name)
    start = _resolve_local(datetime.combine(day, time.min), zone)
    end = _resolve_local(datetime.combine(day + timedelta(days=1), time.min), zone)
    return start, end


def is_in_period(value: datetime, start: datetime, end: datetime) -> bool:
    """Compare exact instants using a nonempty half-open UTC interval."""
    instant, start_utc, end_utc = to_utc(value), to_utc(start), to_utc(end)
    if start_utc >= end_utc:
        raise ValueError("Period start must precede end")
    return start_utc <= instant < end_utc
