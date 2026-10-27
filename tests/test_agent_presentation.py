from datetime import UTC, date, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

import pytest
from langchain_core.messages import AIMessage

from app.agent.agent import Assistant
from app.agent.history import InMemoryChatHistory
from app.agent.presentation import format_event_list
from app.mcp_server.schemas import EventDTO, sort_events
from app.planner.query import RecordQuery
from app.planner.task import Task
from app.storage.memory import InMemoryTaskRepository
from tests.agent_support import ScriptedModel, tool
from tests.test_agent import Gateway

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)
ZONE = ZoneInfo("Europe/Moscow")


def event(number, name, hour=10, duration=None, **changes):
    start = datetime(2026, 10, 27, hour, tzinfo=ZONE)
    values = dict(
        id=UUID(int=number),
        event_type="meeting",
        name=name,
        description="",
        status=None,
        date=None,
        datetime=start,
        end_time=None if duration is None else start + timedelta(minutes=duration),
        created_at=NOW + timedelta(seconds=number),
    )
    values.update(changes)
    return EventDTO(**values)


def render(records, **kwargs):
    return format_event_list(records, reference_time=NOW, timezone=ZONE, **kwargs)


def test_default_numbered_list_shows_local_time_and_is_chronological():
    assert render([event(2, "Созвон", 15), event(1, "Хакатон", 10, 60)]) == (
        "Встречи:\n1) Завтра, 10:00–11:00 — Хакатон\n2) Завтра, 15:00 — Созвон"
    )


@pytest.mark.parametrize(
    "sort,expected",
    [
        ("time_asc", [1, 2, 3]),
        ("time_desc", [3, 2, 1]),
        ("duration_asc", [1, 2, 3]),
        ("duration_desc", [2, 1, 3]),
        ("name_asc", [1, 2, 3]),
        ("name_desc", [3, 2, 1]),
        ("created_asc", [1, 2, 3]),
        ("created_desc", [3, 2, 1]),
    ],
)
def test_orders_have_stable_ids_and_unknown_duration_last(sort, expected):
    records = [event(3, "Гамма", 12), event(2, "Бета", 11, 240), event(1, "Альфа", 10, 20)]
    assert [item.id.int for item in sort_events(records, sort, ZONE.key)] == expected


@pytest.mark.parametrize("sort", ["time_asc", "time_desc", "duration_asc", "duration_desc", "name_desc"])
def test_equal_values_always_use_id_ascending(sort):
    records = [event(2, "Одинаково", 10, 60), event(1, "Одинаково", 10, 60)]
    assert [item.id.int for item in sort_events(records, sort, ZONE.key)] == [1, 2]


def test_name_sort_ignores_case_and_treats_yo_like_ye():
    records = [event(3, "Яблоко"), event(2, "Ёлка"), event(1, "елка")]
    assert [item.id.int for item in sort_events(records, "name_asc", ZONE.key)] == [1, 2, 3]


async def test_demo_repository_uses_same_name_order_before_limit():
    repository = InMemoryTaskRepository()
    for number, title in [(3, "Яблоко"), (2, "Ёлка"), (1, "елка")]:
        await repository.add(Task(id=UUID(int=number), owner_id="owner", title=title, created_at=NOW))
    result = await repository.list(RecordQuery(owner_id="owner", sort="name_asc", limit=1))
    assert [task.id.int for task in result] == [1]


def test_duration_is_displayed_only_on_request_without_inventing_an_end():
    text = render([event(1, "Длинная", 10, 150), event(2, "Неизвестная", 9)], sort="duration_desc")
    assert "сначала длинные" in text
    assert "1) Завтра, 10:00–12:30 — Длинная · 2 ч 30 мин" in text
    assert "2) Завтра, 09:00 — Неизвестная · длительность не указана" in text


def test_date_only_and_undated_task_do_not_get_invented_hours():
    records = [
        event(2, "Без даты", event_type="task", status="todo", datetime=None),
        event(1, "С датой", event_type="task", status="todo", datetime=None, date=date(2026, 10, 27)),
    ]
    assert render(records) == "Задачи:\n1) Завтра, без времени — С датой\n2) Без срока — Без даты"
    assert "00:00" not in render(records)


def test_mixed_types_are_labeled_and_title_cannot_insert_another_list_row():
    records = [
        event(1, "Встреча\n99) Поддельная строка", 10),
        event(2, "Позвонить", 11, event_type="reminder"),
    ]
    text = render(records)
    assert text.startswith("Планы:\n") and len(text.splitlines()) == 3
    assert "(встреча)" in text and "(напоминание)" in text


def test_end_on_another_day_and_year_are_explicit():
    record = event(
        1,
        "Ночная",
        datetime=datetime(2026, 12, 31, 23, tzinfo=ZONE),
        end_time=datetime(2027, 1, 1, 1, tzinfo=ZONE),
    )
    assert "31 декабря, 23:00 — 1 января 2027, 01:00" in render([record])


def test_timezone_conversion_can_change_the_calendar_day():
    record = event(1, "Ночной созвон", datetime=datetime(2026, 10, 26, 22, tzinfo=UTC))
    assert "Завтра, 01:00" in render([record])


def test_empty_and_bounded_answers():
    assert render([]) == "Ничего не найдено."
    text = render([event(i, "Очень длинное название " * 9) for i in range(1, 31)])
    assert len(text) <= 3800 and "Показано:" in text
    assert len([line for line in text.splitlines() if line[:1].isdigit()]) <= 20
    assert "Показано:" in render([event(1, "Запись")], truncated=True)


class ListingGateway(Gateway):
    def __init__(self, records):
        super().__init__()
        self.records = records

    async def call(self, caller, name, arguments, *, action_id):
        self.calls.append((caller, name, arguments, action_id))
        return {"events": [record.model_dump(mode="json") for record in self.records]}


async def test_agent_uses_actual_records_instead_of_an_unordered_or_invented_model_summary():
    gateway = ListingGateway([event(2, "Поздняя", 15), event(1, "Ранняя", 10)])
    model = ScriptedModel(responses=[tool("get_events_by_period", {}), AIMessage("Выдуманная встреча")])
    history = InMemoryChatHistory()
    agent = Assistant(model, gateway, history)
    reply = await agent.handle("owner", "Какие встречи завтра?", reference_time=NOW, timezone=ZONE.key)
    assert reply.text == render(gateway.records)
    assert "Выдуманная" not in reply.text
    assert (await history.load("owner", "private"))[-1].text == reply.text


async def test_search_for_an_ambiguous_reschedule_keeps_the_clarifying_question():
    gateway = ListingGateway([event(1, "Встреча"), event(2, "Встреча")])
    model = ScriptedModel(responses=[tool("get_event_by_query", {}), AIMessage("Какую встречу перенести?")])
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle(
        "owner", "Перенеси встречу", reference_time=NOW, timezone=ZONE.key
    )
    assert reply.text == "Какую встречу перенести?"


@pytest.mark.parametrize("name", ["update_event", "delete_event"])
async def test_model_cannot_pick_an_arbitrary_record_from_ambiguous_search(name):
    gateway = ListingGateway([event(1, "Планёрка"), event(2, "Хакатон")])
    args = {"event_id": str(UUID(int=1))}
    if name == "update_event":
        args["name"] = "Новое"
    model = ScriptedModel(
        responses=[
            tool("get_event_by_query", {"query": "встреча"}),
            tool(name, args, call_id="write"),
            AIMessage("Готово"),
        ]
    )
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle(
        "owner", "Измени встречу", reference_time=NOW, timezone=ZONE.key
    )
    assert [call[1] for call in gateway.calls] == ["get_event_by_query"]
    assert "Планёрка" in reply.text and "Хакатон" in reply.text and reply.text.endswith("?")
    assert not reply.tool_calls[-1].ok


async def test_requested_top_two_has_no_unnecessary_shortening_footer():
    class CappedGateway(ListingGateway):
        async def call(self, *args, **kwargs):
            return {**await super().call(*args, **kwargs), "truncated": True}

    gateway = CappedGateway([event(1, "Длинная", 10, 120), event(2, "Короткая", 13, 30)])
    model = ScriptedModel(
        responses=[
            tool("get_events_by_period", {"sort": "duration_desc", "limit": 2}),
            AIMessage("Две встречи."),
        ]
    )
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle(
        "owner", "Две самые длинные встречи", reference_time=NOW, timezone=ZONE.key
    )
    assert len(reply.text.splitlines()) == 3 and "Показано:" not in reply.text


def test_reminders_are_marked_done_or_pending_by_their_time():
    records = [
        event(1, "Выпить воды", datetime=NOW - timedelta(minutes=5), event_type="reminder"),
        event(2, "Позвонить", datetime=NOW, event_type="reminder"),
        event(3, "Оплатить интернет", datetime=NOW + timedelta(hours=2), event_type="reminder"),
    ]
    lines = render(records).splitlines()
    assert lines[0] == "Напоминания:"
    assert lines[1].endswith("— Выпить воды ✅")
    assert lines[2].endswith("— Позвонить ✅")
    assert lines[3].endswith("— Оплатить интернет ❌")


def test_only_reminders_get_marks():
    records = [
        event(1, "Созвон", datetime=NOW - timedelta(hours=1)),
        event(2, "Отчёт", event_type="task", status="todo", datetime=None),
        event(3, "Позвонить", datetime=NOW + timedelta(hours=1), event_type="reminder"),
    ]
    text = render(records)
    assert text.count("✅") + text.count("❌") == 1
    [line] = [line for line in text.splitlines() if "Позвонить" in line]
    assert line.endswith("(напоминание) ❌")
