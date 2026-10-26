"""Assertions on the tool calls an agent made for one eval case.

A check gets the arguments of the matched tool call and returns an error
message, or None when the call is correct.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any
from uuid import UUID

Check = Callable[[dict[str, Any]], str | None]


def uuid_argument(key: str) -> Check:
    """Generated PostgreSQL IDs are UUIDs, never the old fixture's integer positions."""

    def check(args: dict[str, Any]) -> str | None:
        value = args.get(key)
        if not isinstance(value, str):
            return f"{key} должен быть UUID из результата инструмента"
        try:
            UUID(value)
        except ValueError:
            return f"{key} должен быть UUID из результата инструмента"
        return None

    return check


END_TOLERANCE = timedelta(minutes=1)

# Answers are spoken by TTS, so formatting characters are read aloud or break intonation.
_MARKDOWN_RE = re.compile(r"\*\*|__|^#+\s|^\s*[-*•]\s|`", re.MULTILINE)
_EMOJI_RE = re.compile("[\U0001f300-\U0001faff☀-➿]")


# "через два часа" instead of a clock time: the user hears no moment to check, and it hides a wrong day.
_RELATIVE_TIME_RE = re.compile(r"\bчерез\s+(?:[\wё-]+\s+)?(?:полчаса|час|минут)", re.IGNORECASE)
_CLOCK_RE = re.compile(r"\b\d{1,2}:\d{2}\b")


def speech_issues(answer: str, *, allow_lists: bool = False) -> list[str]:
    issues = []
    if _MARKDOWN_RE.search(answer):
        issues.append("в ответе markdown — плохо для озвучивания")
    if not allow_lists and "\n" in answer.strip():
        issues.append("в ответе переносы строк")
    if _EMOJI_RE.search(answer):
        issues.append("в ответе эмодзи")
    if _RELATIVE_TIME_RE.search(answer) and not _CLOCK_RE.search(answer):
        issues.append("время названо относительно («через …»), а не по часам")
    return issues


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


def near(key: str, expected_iso: str, tolerance: timedelta = timedelta(minutes=1)) -> Check:
    """Moment within a tolerance: "через 2 часа" may keep or drop the seconds of "now"."""
    expected = datetime.fromisoformat(expected_iso)

    def check(args: dict[str, Any]) -> str | None:
        actual = _parse(args.get(key))
        if actual is None:
            return f"{key}={args.get(key)!r}: не ISO 8601 с часовым поясом"
        ok = abs(actual - expected) <= tolerance
        return None if ok else f"{key}={actual.isoformat()}, ожидалось около {expected_iso}"

    return check


def one_of(key: str, *allowed: Any) -> Check:
    def check(args: dict[str, Any]) -> str | None:
        return None if args.get(key) in allowed else f"{key}={args.get(key)!r}, ожидалось одно из {allowed}"

    return check


def on_date(key: str, *expected: str) -> Check:
    """Local date of the moment is one of `expected`: several when the phrase is ambiguous."""
    expected_dates = {date.fromisoformat(e) for e in expected}

    def check(args: dict[str, Any]) -> str | None:
        actual = _parse(args.get(key))
        if actual is None:
            return f"{key}={args.get(key)!r}: не ISO 8601 с часовым поясом"
        wanted = " или ".join(expected)
        return None if actual.date() in expected_dates else f"{key}: дата {actual.date()}, ожидалась {wanted}"

    return check


def due_on(*expected: str) -> Check:
    """A task date without time: `date` is one of `expected` and no `datetime` is invented (AGENTS §7)."""

    def check(args: dict[str, Any]) -> str | None:
        if args.get("datetime"):
            return f"datetime={args['datetime']!r}: для задачи без времени ожидалась только date"
        actual = args.get("date")
        wanted = " или ".join(expected)
        return None if actual in expected else f"date={actual!r}, ожидалась {wanted}"

    return check


def on_day(*expected: str) -> Check:
    """The record falls on one of `expected` days, by `date` or by the local date of `datetime`.

    For phrases where both a dated task and a timed reminder are acceptable.
    """
    by_datetime = on_date("datetime", *expected)

    def check(args: dict[str, Any]) -> str | None:
        if args.get("date") and not args.get("datetime"):
            return None if args["date"] in expected else f"date={args['date']!r}, ожидалась {expected}"
        return by_datetime(args)

    return check


def no_time() -> Check:
    """A task without a deadline: neither `date` nor `datetime`."""

    def check(args: dict[str, Any]) -> str | None:
        extra = {k: args[k] for k in ("date", "datetime") if args.get(k)}
        return f"задача без срока, но передано {extra}" if extra else None

    return check


def covers(start_iso: str, end_iso: str, max_extra: timedelta = timedelta(days=1)) -> Check:
    """Half-open range [start_time, end_time) includes the expected period and is not much wider.

    Periods follow AGENTS §7: the end is not included, so a day ends at 00:00 of the next day.
    An end at 23:59:59 misses the last second of the period and fails.
    """
    need_start = datetime.fromisoformat(start_iso)
    need_end = datetime.fromisoformat(end_iso)

    def check(args: dict[str, Any]) -> str | None:
        start, end = _parse(args.get("start_time")), _parse(args.get("end_time"))
        if start is None or end is None:
            return f"диапазон {args.get('start_time')!r}..{args.get('end_time')!r}: не ISO 8601"
        if start > need_start or end < need_end:
            return f"диапазон [{start.isoformat()}, {end.isoformat()}) не покрывает [{start_iso}, {end_iso})"
        if (need_start - start) + (end - need_end) > max_extra:
            return f"диапазон [{start.isoformat()}, {end.isoformat()}) слишком широкий"
        return None

    return check


def types_include(*types: str) -> Check:
    def check(args: dict[str, Any]) -> str | None:
        requested = args.get("event_types") or []
        if not requested:  # no filter means "all types"
            return None
        missing = [t for t in types if t not in requested]
        return f"event_types={requested}, нет {missing}" if missing else None

    return check


def mentions(key: str, *words: str) -> Check:
    """Free-text argument contains each word stem (case-insensitive); "a|b" accepts any alternative."""

    def check(args: dict[str, Any]) -> str | None:
        text = str(args.get(key) or "").lower()
        missing = [w for w in words if not any(alt.lower() in text for alt in w.split("|"))]
        return f"{key}={text!r}, нет {missing}" if missing else None

    return check


@dataclass(frozen=True)
class Expect:
    """Expected behaviour on the last turn of a case.

    tool=None means the agent must not create anything (clarifying question or small talk).
    """

    tool: str | None
    checks: list[Check] = field(default_factory=list)
    # Each entry must occur in the answer; "a|b" accepts any of the alternatives.
    answer_mentions: list[str] = field(default_factory=list)
    # Tools that must not be called on the last turn (by default nothing may be created or deleted
    # when tool=None).
    forbid: list[str] = field(default_factory=list)
    # Several records in one phrase: each group of checks must match some call of `tool`.
    also: list[list[Check]] = field(default_factory=list)
