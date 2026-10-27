"""Dates of «через неделю / месяц / год» are computed in code and given to the model (role 3)."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from app.llm.prompts import add_months, render_system_prompt

MOSCOW = ZoneInfo("Europe/Moscow")


def _block(now: datetime) -> str:
    prompt = render_system_prompt(now, MOSCOW)
    start = prompt.index("Через сколько дней (дата")
    return prompt[start:]


def test_date_offsets_are_ready_values() -> None:
    block = _block(datetime(2026, 10, 19, 12, tzinfo=MOSCOW))
    for line in (
        "через неделю: 2026-10-26 (понедельник)",
        "через месяц: 2026-11-19 (четверг)",
        "через полгода: 2027-04-19 (понедельник)",
        "через год: 2027-10-19 (вторник)",
    ):
        assert line in block
    assert "T" not in block  # a date, never a moment


@pytest.mark.parametrize(
    ("day", "months", "expected"),
    [
        (date(2026, 1, 31), 1, date(2026, 2, 28)),
        (date(2028, 1, 31), 1, date(2028, 2, 29)),  # leap year
        (date(2028, 2, 29), 12, date(2029, 2, 28)),
        (date(2026, 11, 30), 3, date(2027, 2, 28)),
        (date(2026, 12, 15), 1, date(2027, 1, 15)),
    ],
)
def test_months_are_calendar_months_clamped_to_month_end(day: date, months: int, expected: date) -> None:
    assert add_months(day, months) == expected


def test_date_offsets_follow_the_users_day_not_utc() -> None:
    """At 01:30 in Yekaterinburg it is still the previous day in UTC; «через неделю» counts from local."""
    yekb = ZoneInfo("Asia/Yekaterinburg")
    prompt = render_system_prompt(datetime.fromisoformat("2026-10-19T20:30:00+00:00"), yekb)
    assert "через неделю: 2026-10-27" in prompt


def test_next_weekday_is_a_ready_date_after_today() -> None:
    """«В пятницу» on Monday 19.10 is 23.10; on Friday it is the next Friday, not today."""
    monday = render_system_prompt(datetime(2026, 10, 19, 12, tzinfo=MOSCOW), MOSCOW)
    assert "- в пятницу: 2026-10-23" in monday
    assert "- во вторник: 2026-10-20" in monday
    friday = render_system_prompt(datetime(2026, 10, 23, 12, tzinfo=MOSCOW), MOSCOW)
    assert "- в пятницу: 2026-10-30" in friday
