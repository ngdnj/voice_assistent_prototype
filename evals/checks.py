"""Assertions on the tool calls an agent made for one eval case.

A check gets the arguments of the matched tool call and returns an error
message, or None when the call is correct.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

Check = Callable[[dict[str, Any]], str | None]


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def equals(key: str, expected: Any) -> Check:
    def check(args: dict[str, Any]) -> str | None:
        return None if args.get(key) == expected else f"{key}={args.get(key)!r}, ожидалось {expected!r}"

    return check


def at(key: str, expected_iso: str) -> Check:
    """Exact moment, compared as aware datetimes so the offset notation does not matter."""
    expected = datetime.fromisoformat(expected_iso)

    def check(args: dict[str, Any]) -> str | None:
        actual = _parse(args.get(key))
        if actual is None:
            return f"{key}={args.get(key)!r}: не ISO 8601 с часовым поясом"
        return None if actual == expected else f"{key}={actual.isoformat()}, ожидалось {expected_iso}"

    return check


def on_date(key: str, expected: str) -> Check:
    expected_date = date.fromisoformat(expected)

    def check(args: dict[str, Any]) -> str | None:
        actual = _parse(args.get(key))
        if actual is None:
            return f"{key}={args.get(key)!r}: не ISO 8601 с часовым поясом"
        return (
            None if actual.date() == expected_date else f"{key}: дата {actual.date()}, ожидалась {expected}"
        )

    return check


def covers(start_iso: str, end_iso: str, max_extra: timedelta = timedelta(days=1)) -> Check:
    """Range [start_time, end_time] includes the expected period and is not much wider."""
    need_start = datetime.fromisoformat(start_iso)
    need_end = datetime.fromisoformat(end_iso)

    def check(args: dict[str, Any]) -> str | None:
        start, end = _parse(args.get("start_time")), _parse(args.get("end_time"))
        if start is None or end is None:
            return f"диапазон {args.get('start_time')!r}..{args.get('end_time')!r}: не ISO 8601"
        if start > need_start or end < need_end:
            return f"диапазон {start.isoformat()}..{end.isoformat()} не покрывает {start_iso}..{end_iso}"
        if (need_start - start) + (end - need_end) > max_extra:
            return f"диапазон {start.isoformat()}..{end.isoformat()} слишком широкий"
        return None

    return check


def types_include(*types: str) -> Check:
    def check(args: dict[str, Any]) -> str | None:
        requested = args.get("event_type") or []
        if not requested:  # empty list means "all types"
            return None
        missing = [t for t in types if t not in requested]
        return f"event_type={requested}, нет {missing}" if missing else None

    return check


def mentions(key: str, *words: str) -> Check:
    """Free-text argument contains each word stem (case-insensitive)."""

    def check(args: dict[str, Any]) -> str | None:
        text = str(args.get(key) or "").lower()
        missing = [w for w in words if w.lower() not in text]
        return f"{key}={text!r}, нет {missing}" if missing else None

    return check


@dataclass(frozen=True)
class Expect:
    """Expected behaviour on the last turn of a case.

    tool=None means the agent must not create anything (clarifying question or small talk).
    """

    tool: str | None
    checks: list[Check] = field(default_factory=list)
    answer_mentions: list[str] = field(default_factory=list)
