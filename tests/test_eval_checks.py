from datetime import timedelta

from assistant.llm.agent import AgentReply, ToolCall
from evals.cases import CASES
from evals.checks import Expect, at, covers, mentions, on_date, types_include
from evals.run import _evaluate

DAY = covers("2026-10-19T00:00:00+03:00", "2026-10-19T23:59:59+03:00", max_extra=timedelta(hours=1))


def test_at_compares_moments_across_offsets() -> None:
    check = at("datetime", "2026-10-20T10:00:00+03:00")
    assert check({"datetime": "2026-10-20T07:00:00+00:00"}) is None
    assert check({"datetime": "2026-10-20T10:00:00"}) is not None  # naive is a failure
    assert check({"datetime": "2026-10-20T11:00:00+03:00"}) is not None


def test_on_date() -> None:
    assert on_date("datetime", "2026-10-23")({"datetime": "2026-10-23T23:59:00+03:00"}) is None
    assert on_date("datetime", "2026-10-23")({"datetime": None}) is not None


def test_covers_rejects_short_and_too_wide_ranges() -> None:
    ok = {"start_time": "2026-10-19T00:00:00+03:00", "end_time": "2026-10-19T23:59:59+03:00"}
    short = {"start_time": "2026-10-19T12:00:00+03:00", "end_time": "2026-10-19T23:59:59+03:00"}
    wide = {"start_time": "2026-10-19T00:00:00+03:00", "end_time": "2026-10-26T00:00:00+03:00"}
    assert DAY(ok) is None
    assert DAY(short) is not None
    assert DAY(wide) is not None


def test_types_include_treats_empty_list_as_all() -> None:
    assert types_include("meeting")({"event_type": []}) is None
    assert types_include("meeting")({"event_type": ["task"]}) is not None


def test_mentions_is_case_insensitive() -> None:
    assert mentions("name", "иван")({"name": "Позвонить Ивану"}) is None


def test_evaluate_clarification_case_fails_when_event_created() -> None:
    case = next(c for c in CASES if c.id == "meeting_without_time_asks")
    asked = AgentReply("На какое время поставить встречу?")
    created = AgentReply("Готово", [ToolCall("create_event", {"event_type": "meeting"})])
    assert _evaluate(case, asked) == []
    assert _evaluate(case, created)


def test_all_cases_reference_known_tools() -> None:
    known = {None, "create_event", "get_event", "get_event_by_query"}
    assert len({c.id for c in CASES}) == len(CASES)
    for case in CASES:
        assert isinstance(case.expect, Expect)
        assert case.expect.tool in known, case.id
