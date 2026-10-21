from datetime import timedelta

import pytest

# The eval harness drives the agent runner of role 1 (app/agent), which is not in the
# repository yet. Until it lands these tests are skipped with this reason instead of
# failing the whole run.
pytest.importorskip("app.agent.agent", reason="app/agent (role 1) is not in the repository yet")

from app.agent.agent import AgentReply, ToolCall

from evals.cases import CASES
from evals.checks import (
    Expect,
    at,
    covers,
    due_on,
    mentions,
    near,
    no_time,
    on_date,
    on_day,
    speech_issues,
    types_include,
)
from evals.run import _evaluate

DAY = covers("2026-10-19T00:00:00+03:00", "2026-10-20T00:00:00+03:00", max_extra=timedelta(hours=1))


def test_at_compares_moments_across_offsets() -> None:
    check = at("datetime", "2026-10-20T10:00:00+03:00")
    assert check({"datetime": "2026-10-20T07:00:00+00:00"}) is None
    assert check({"datetime": "2026-10-20T10:00:00"}) is not None  # naive is a failure
    assert check({"datetime": "2026-10-20T11:00:00+03:00"}) is not None


def test_on_date() -> None:
    assert on_date("datetime", "2026-10-23")({"datetime": "2026-10-23T23:59:00+03:00"}) is None
    assert on_date("datetime", "2026-10-23")({"datetime": None}) is not None


def test_covers_rejects_short_and_too_wide_ranges() -> None:
    ok = {"start_time": "2026-10-19T00:00:00+03:00", "end_time": "2026-10-20T00:00:00+03:00"}
    short = {"start_time": "2026-10-19T12:00:00+03:00", "end_time": "2026-10-20T00:00:00+03:00"}
    wide = {"start_time": "2026-10-19T00:00:00+03:00", "end_time": "2026-10-26T00:00:00+03:00"}
    assert DAY(ok) is None
    assert DAY(short) is not None
    assert DAY(wide) is not None


def test_types_include_treats_empty_list_as_all() -> None:
    assert types_include("meeting")({"event_types": []}) is None
    assert types_include("meeting")({}) is None
    assert types_include("meeting")({"event_types": ["task"]}) is not None


def test_mentions_is_case_insensitive() -> None:
    assert mentions("name", "иван")({"name": "Позвонить Ивану"}) is None


def test_evaluate_clarification_case_fails_when_event_created() -> None:
    case = next(c for c in CASES if c.id == "meeting_without_time_asks")
    asked = AgentReply("На какое время поставить встречу?")
    created = AgentReply("Готово", [ToolCall("create_event", {"event_type": "meeting"})])
    assert _evaluate(case, asked) == []
    assert _evaluate(case, created)


def test_all_cases_reference_known_tools() -> None:
    known = {None, "create_event", "get_events_by_period", "get_event_by_query", "delete_event"}
    assert len({c.id for c in CASES}) == len(CASES)
    for case in CASES:
        assert isinstance(case.expect, Expect)
        assert case.expect.tool in known, case.id


def test_covers_is_half_open_and_rejects_inclusive_end() -> None:
    """AGENTS §7: a day is [00:00, next 00:00); 23:59:59 loses the last second of the day."""
    assert (
        DAY({"start_time": "2026-10-19T00:00:00+03:00", "end_time": "2026-10-19T23:59:59+03:00"}) is not None
    )
    assert (
        DAY({"start_time": "2026-10-19T00:00:00+03:00", "end_time": "2026-10-19T23:59:00+03:00"}) is not None
    )


def test_due_on_requires_date_without_invented_time() -> None:
    check = due_on("2026-10-23")
    assert check({"date": "2026-10-23"}) is None
    assert check({"datetime": "2026-10-23T23:59:00+03:00"}) is not None  # time the user did not say
    assert check({"date": "2026-10-23", "datetime": "2026-10-23T00:00:00+03:00"}) is not None
    assert check({"date": "2026-10-24"}) is not None


def test_on_day_accepts_date_or_moment() -> None:
    check = on_day("2026-10-25")
    assert check({"date": "2026-10-25"}) is None
    assert check({"datetime": "2026-10-25T12:00:00+03:00"}) is None
    assert check({"date": "2026-10-26"}) is not None


def test_no_time() -> None:
    assert no_time()({"name": "x"}) is None
    assert no_time()({"date": "2026-10-23"}) is not None


def test_speech_issues() -> None:
    assert speech_issues("Завтра в 15:00 встреча с командой.") == []
    assert speech_issues("Сегодня:\n- **Созвон** в 16:00")
    assert speech_issues("Готово 👍")


def test_speech_issues_require_clock_time_for_relative_moments() -> None:
    assert speech_issues("Хорошо, напомню через два часа.")
    assert speech_issues("Напомню через полчаса выключить машину.")
    assert speech_issues("Через час позвоните маме.")
    assert speech_issues("Напомню завтра в 00:23 позвонить маме.") == []
    assert speech_issues("Напомню через два часа, в 14:00.") == []


def test_near_accepts_dropped_seconds_and_rejects_wrong_day() -> None:
    check = near("datetime", "2026-10-20T00:23:55+03:00")
    assert check({"datetime": "2026-10-20T00:23:00+03:00"}) is None
    assert check({"datetime": "2026-10-19T23:59:00+03:00"}) is not None


def test_offsets_cross_midnight() -> None:
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from app.llm.prompts import render_system_prompt

    moscow = ZoneInfo("Europe/Moscow")
    prompt = render_system_prompt(datetime(2026, 10, 19, 22, 23, 55, tzinfo=moscow), moscow)
    assert "через 2 часа: 2026-10-20T00:23:00+03:00 (завтра)" in prompt
