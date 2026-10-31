"""Saved writes remain visible when the model loops, stops or fails afterwards."""

from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage

from app.agent.agent import PARTIAL_WRITE_NOTE, Assistant
from app.agent.history import InMemoryChatHistory
from tests.agent_support import ScriptedModel, tool
from tests.test_agent import Gateway

NOW = datetime(2026, 10, 31, 10, 26, tzinfo=UTC)
TIME = {"reference_time": NOW, "timezone": "Europe/Moscow"}
REQUEST = "Поставь напоминание проверить отчёт завтра, послезавтра, после послезавтра, в понедельник в 14:00"


def create(day, *, name="Проверить отчёт", call_id=None, **extra):
    return tool(
        arguments={
            "event_type": "reminder",
            "name": name,
            "datetime": f"2026-11-{day:02}T14:00:00+03:00",
            **extra,
        },
        call_id=call_id or f"day-{day}",
    )


class SavedGateway(Gateway):
    async def call(self, caller, name, arguments, *, action_id):
        if (
            name == "create_event"
            and arguments.get("event_type") in {"meeting", "reminder"}
            and not arguments.get("datetime")
        ):
            self.calls.append((caller, name, arguments, action_id))
            return {"error": {"code": "validation_error", "message": "Нужно время", "field": "datetime"}}
        if name == "create_event" and arguments.get("name") == "Отклонено":
            self.calls.append((caller, name, arguments, action_id))
            return {"error": {"code": "validation_error", "message": "Проверьте дату"}}
        result = await super().call(caller, name, arguments, action_id=action_id)
        if name != "create_event":
            return result
        return {
            "created": {
                "id": result["created"]["id"],
                "event_type": arguments["event_type"],
                "name": arguments["name"],
                "description": arguments.get("description", ""),
                "datetime": arguments.get("datetime"),
                "date": arguments.get("date"),
                "end_time": arguments.get("end_time"),
                "status": None,
                "created_at": NOW.isoformat(),
            }
        }


async def test_screenshot_repeated_monday_stops_with_three_actual_reminders_and_replays():
    gateway, history = SavedGateway(), InMemoryChatHistory()
    responses = [create(day, call_id=f"call-{i}") for i, day in enumerate([1, 2, 3, 2, 2, 2, 2, 3, 2, 2])]
    model = ScriptedModel(responses=responses)
    agent = Assistant(model, gateway, history)
    reply = await agent.handle("owner", REQUEST, operation_id="screenshot", **TIME)
    assert len(gateway.calls) == 3
    assert len(model.seen) == 5
    assert reply.text.startswith("Созданы напоминания:")
    assert reply.text.count("Проверить отчёт") == 3
    assert (
        "Завтра, 14:00" in reply.text and "2 ноября, 14:00" in reply.text and "3 ноября, 14:00" in reply.text
    )
    assert "Действие выполнено" not in reply.text
    assert PARTIAL_WRITE_NOTE in reply.text
    repeated = await agent.handle("owner", REQUEST, operation_id="screenshot", **TIME)
    assert repeated.text == reply.text and len(gateway.calls) == 3 and len(model.seen) == 5


async def test_step_limit_lists_only_the_record_that_was_saved():
    gateway = SavedGateway()
    reply = await Assistant(
        ScriptedModel(responses=[create(1)]), gateway, InMemoryChatHistory(), max_steps=1
    ).handle("owner", REQUEST, **TIME)
    assert "Завтра, 14:00" in reply.text
    assert "2 ноября" not in reply.text and "3 ноября" not in reply.text
    assert PARTIAL_WRITE_NOTE in reply.text and len(gateway.calls) == 1


async def test_normal_multi_create_answer_uses_records_instead_of_fabricated_model_summary():
    model = ScriptedModel(
        responses=[create(1), create(2), AIMessage("Все четыре напоминания созданы на 18:00.")]
    )
    reply = await Assistant(model, SavedGateway(), InMemoryChatHistory()).handle("owner", REQUEST, **TIME)
    assert reply.text.count("Проверить отчёт") == 2
    assert "18:00" not in reply.text and "четыре" not in reply.text
    assert PARTIAL_WRITE_NOTE not in reply.text


async def test_equivalent_utc_moment_and_default_description_do_not_create_a_duplicate():
    gateway = SavedGateway()
    repeated = create(
        1, name=" Проверить отчёт ", call_id="same-utc", description="", datetime="2026-11-01T11:00:00Z"
    )
    model = ScriptedModel(responses=[create(1), repeated, AIMessage("Готово.")])
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle("owner", REQUEST, **TIME)
    assert len(gateway.calls) == 1 and reply.text.count("Проверить отчёт") == 1
    assert reply.tool_calls[-1].result["already_applied"] is True


async def test_failed_write_is_not_listed_as_saved():
    model = ScriptedModel(responses=[create(1), create(2, name="Отклонено"), AIMessage("Всё сохранено.")])
    reply = await Assistant(model, SavedGateway(), InMemoryChatHistory()).handle("owner", REQUEST, **TIME)
    assert "Завтра, 14:00" in reply.text
    assert "Отклонено" not in reply.text and "2 ноября" not in reply.text
    assert "ошибкой" in reply.text and "Всё сохранено" not in reply.text


async def test_clarification_does_not_hide_an_earlier_saved_record():
    missing_time = tool(arguments={"event_type": "meeting", "name": "Обсуждение"}, call_id="missing")
    model = ScriptedModel(responses=[create(1), missing_time, AIMessage("Во сколько начнётся встреча?")])
    reply = await Assistant(model, SavedGateway(), InMemoryChatHistory()).handle(
        "owner", "Напомни завтра в 14:00 проверить отчёт и создай встречу Обсуждение", **TIME
    )
    assert "Проверить отчёт" in reply.text
    assert reply.text.endswith("?")


class FailingModel(ScriptedModel):
    def _generate(self, *args, **kwargs):
        if self.seen:
            raise ConnectionError("synthetic model failure")
        return super()._generate(*args, **kwargs)


async def test_provider_failure_after_commit_returns_saved_record_without_another_write():
    gateway, history = SavedGateway(), InMemoryChatHistory()
    agent = Assistant(FailingModel(responses=[create(1)]), gateway, history)
    reply = await agent.handle("owner", REQUEST, operation_id="provider-failed", **TIME)
    assert len(gateway.calls) == 1 and "Завтра, 14:00" in reply.text and PARTIAL_WRITE_NOTE in reply.text
    replay = await agent.handle("owner", REQUEST, operation_id="provider-failed", **TIME)
    assert replay.text == reply.text and len(gateway.calls) == 1


async def test_provider_failure_without_confirmed_writes_propagates():
    class ImmediatelyFailing(ScriptedModel):
        def _generate(self, *args, **kwargs):
            raise ConnectionError("synthetic failure")

    with pytest.raises(ConnectionError):
        await Assistant(ImmediatelyFailing(responses=[]), SavedGateway(), InMemoryChatHistory()).handle(
            "owner", REQUEST, **TIME
        )


async def test_read_step_limit_does_not_claim_any_write():
    model = ScriptedModel(responses=[tool("get_event_by_query", {"query": "отчёт"})])
    reply = await Assistant(model, SavedGateway(), InMemoryChatHistory(), max_steps=1).handle(
        "owner", "Найди отчёт", **TIME
    )
    assert "число шагов" in reply.text and "Созданы" not in reply.text
