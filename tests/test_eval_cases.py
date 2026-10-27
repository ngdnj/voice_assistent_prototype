from collections import Counter
from zoneinfo import ZoneInfo

from app.agent.agent import AgentReply, ToolCall
from evals.cases import CASES, Case
from evals.checks import Expect, at, mentions, on_date
from evals.run import _evaluate
from evals.stats import describe, summarize

MVP_CLASSES = ("create", "period", "clarify", "search", "general")


def test_each_class_has_20_to_31_cases_and_both_splits() -> None:
    measured = [c for c in CASES if c.split != "debug"]  # debug cases are not the §15 dataset
    per_class = Counter(c.category for c in measured)
    for category in MVP_CLASSES:
        assert 20 <= per_class[category] <= 31, (category, per_class[category])
        splits = {c.split for c in measured if c.category == category}
        assert splits == {"tune", "holdout"}, category


def test_case_ids_are_unique_and_zones_valid() -> None:
    assert len({c.id for c in CASES}) == len(CASES)
    for case in CASES:
        ZoneInfo(case.timezone)
        assert case.now.tzinfo is not None, case.id


def test_mentions_and_on_date_accept_alternatives() -> None:
    assert mentions("text", "отч|продаж")({"text": "продажи"}) is None
    assert mentions("text", "отч|продаж")({"text": "бюджет"}) is not None
    check = on_date("datetime", "2026-10-23", "2026-10-30")
    assert check({"datetime": "2026-10-30T12:00:00+03:00"}) is None
    assert check({"datetime": "2026-10-24T12:00:00+03:00"}) is not None


def _case(expect: Expect) -> Case:
    return Case("x", "create", ["..."], expect)


def _reply(*calls: ToolCall) -> AgentReply:
    return AgentReply(text="Готово.", tool_calls=list(calls))


def test_also_groups_need_a_matching_call_each() -> None:
    expect = Expect(
        "create_event",
        also=[
            [at("datetime", "2026-10-20T09:00:00+03:00")],
            [at("datetime", "2026-10-20T18:00:00+03:00")],
        ],
    )
    nine = ToolCall("create_event", {"datetime": "2026-10-20T09:00:00+03:00"})
    six = ToolCall("create_event", {"datetime": "2026-10-20T18:00:00+03:00"})
    assert _evaluate(_case(expect), _reply(nine, six)) == []
    [error] = _evaluate(_case(expect), _reply(nine))
    assert "нет подходящего create_event" in error


def test_no_expected_tool_forbids_create_and_delete() -> None:
    errors = _evaluate(_case(Expect(None)), _reply(ToolCall("delete_event", {"event_id": 1})))
    assert errors and "delete_event" in errors[0]
    assert _evaluate(_case(Expect(None)), _reply(ToolCall("get_event_by_query", {"text": "x"}))) == []


def test_summarize_reports_mean_median_p95_and_skips_infra_errors() -> None:
    def row(passed: bool, latency: float, infra: bool = False) -> dict[str, object]:
        return {
            "category": "create",
            "passed": passed,
            "infra_error": infra,
            "latency_s": latency,
            "cost_rub": 0.1,
            "input_tokens": 1000,
            "output_tokens": 50,
        }

    summary = summarize([row(True, 1.0), row(True, 2.0), row(False, 6.0), row(False, 0.0, infra=True)])
    assert (summary["passed"], summary["n"], summary["infra_errors"]) == (2, 3, 1)
    assert summary["latency_s"]["mean"] == 3.0
    assert summary["latency_s"]["median"] == 2.0
    assert summary["by_category"] == {"create": {"passed": 2, "n": 3}}
    assert describe([]) == {"mean": 0.0, "median": 0.0, "p95": 0.0}


def test_behavior_score_excuses_only_rejected_changes() -> None:
    """«31 ноября»: create_event was tried, the check rejected it, the model asked — no record changed."""
    case = _case(Expect(None, answer_mentions=["нет такого"]))
    rejected = ToolCall("create_event", {"datetime": "2026-11-31T10:00:00+03:00"}, {"error": {"code": "x"}})
    reply = _reply(rejected)
    reply.text = "31 ноября нет такого дня, какую дату поставить?"
    assert _evaluate(case, reply)  # strict: create_event must not be called at all
    assert _evaluate(case, reply, by_behavior=True) == []

    saved = ToolCall("create_event", {"datetime": "2026-11-30T10:00:00+03:00"}, {"created": {"id": "1"}})
    reply = _reply(saved)
    reply.text = "31 ноября нет такого дня, поставил на 30-е."
    assert _evaluate(case, reply, by_behavior=True)  # a record did change


def test_summarize_counts_behavior_and_cached_tokens() -> None:
    rows = [
        {"category": "clarify", "passed": False, "passed_behavior": True, "cached_input_tokens": 2000},
        # A report from before the behaviour score: falls back to the strict result, no cache.
        {"category": "clarify", "passed": True},
    ]
    for row in rows:
        row.update(infra_error=False, latency_s=1.0, cost_rub=0.0, input_tokens=200, output_tokens=50)
    summary = summarize(rows)
    assert (summary["passed"], summary["passed_behavior"]) == (1, 2)
    assert summary["cached_input_tokens"]["mean"] == 1000
    assert summary["total_input_tokens"]["mean"] == 1200
