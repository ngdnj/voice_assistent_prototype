import pytest

# The eval harness drives the agent runner of role 1 (app/agent), which is not in the
# repository yet. Until it lands these tests are skipped instead of failing the run.
pytest.importorskip("app.agent.agent", reason="app/agent (role 1) is not in the repository yet")

from app.agent.agent import AgentReply, ToolCall
from evals.cases import CASES
from evals.run import build_trace, format_trace


def test_trace_keeps_every_turn_call_and_server_answer() -> None:
    replies = [
        AgentReply("Во сколько поставить встречу?"),
        AgentReply(
            "Встреча с Иваном завтра в 16:00.",
            [
                ToolCall(
                    "create_event",
                    {"event_type": "meeting", "datetime": "2026-10-20T16:00:00+03:00"},
                    {"created": {"id": 1}},
                )
            ],
        ),
    ]
    trace = build_trace(["Поставь встречу с Иваном завтра", "в 16"], replies)

    assert [step["user"] for step in trace] == ["Поставь встречу с Иваном завтра", "в 16"]
    assert trace[0]["calls"] == []
    assert trace[1]["calls"][0]["ok"] is True
    assert '"created"' in trace[1]["calls"][0]["result"]


def test_trace_marks_rejected_calls() -> None:
    rejected = ToolCall("create_event", {"datetime": "2026-11-31T10:00:00+03:00"}, {"error": "bad date"})
    lines = format_trace(build_trace(["31 ноября встреча"], [AgentReply("Такой даты нет", [rejected])]))
    text = "\n".join(lines)
    assert "ОШИБКА" in text and "bad date" in text
    assert "ответ: Такой даты нет" in text


def test_trace_truncates_long_results() -> None:
    long = ToolCall("get_events_by_period", {}, {"events": ["x" * 2000]})
    [step] = build_trace(["что сегодня"], [AgentReply("…", [long])])
    assert len(step["calls"][0]["result"]) <= 300


def test_debug_cases_are_a_separate_split_and_unique() -> None:
    debug = [c for c in CASES if c.id.startswith("dbg_")]
    assert len(debug) >= 20
    assert all(c.split == "debug" for c in debug)
    assert len({c.id for c in CASES}) == len(CASES)
