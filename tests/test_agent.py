from datetime import UTC, datetime

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from app.agent.agent import Assistant, TemporaryToolError
from app.agent.history import ConversationBusyError, InMemoryChatHistory
from app.agent.mcp import McpGateway
from app.agent.tools import model_tool_schemas
from app.planner.operation import OperationConflictError
from tests.agent_support import ScriptedModel, tool

NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)
TIME = {"reference_time": NOW, "timezone": "Europe/Moscow"}


class Gateway:
    def __init__(self):
        self.calls = []
        self.results = {}
        self.lost_response = False
        self.temporary = False

    async def list_tools(self, caller):
        return [
            {"name": name, "description": name, "parameters": {"type": "object", "properties": {}}}
            for name in (
                "create_event",
                "get_events_by_period",
                "get_event_by_query",
                "reschedule_meeting",
                "update_event",
                "delete_event",
            )
        ]

    async def call(self, caller, name, arguments, *, action_id):
        self.calls.append((caller, name, arguments, action_id))
        if self.temporary:
            return {"error": {"code": "temporary_unavailable"}}
        if name == "update_event":
            return {"updated": {"id": arguments["event_id"]}}
        if name == "delete_event":
            return {"deleted": {"id": arguments["event_id"]}}
        if name == "reschedule_meeting":
            return {"updated": {"id": arguments["meeting_id"], "datetime": arguments.get("datetime")}}
        if name != "create_event":
            return {"events": []}
        result = self.results.setdefault((caller.owner_id, action_id), {"created": {"id": action_id}})
        if self.lost_response:
            self.lost_response = False
            raise ConnectionError("response lost after commit")
        return result


async def test_tool_result_drives_answer_and_operation_replay():
    model = ScriptedModel(responses=[tool(), AIMessage("Задача создана.")])
    gateway, history = Gateway(), InMemoryChatHistory()
    agent = Assistant(model, gateway, history)
    reply = await agent.handle("owner", "Создай задачу", operation_id="turn-1", **TIME)
    assert reply.tool_calls[0].ok
    assert isinstance(model.seen[-1][-1], ToolMessage)
    assert len(gateway.results) == 1
    repeated = await agent.handle("owner", "Создай задачу", operation_id="turn-1", **TIME)
    assert repeated.text == reply.text
    assert len(model.seen) == 2
    with pytest.raises(OperationConflictError):
        await agent.handle("owner", "Другие данные", operation_id="turn-1", **TIME)


async def test_restart_resumes_persisted_call_with_same_action_after_lost_response():
    history, gateway = InMemoryChatHistory(), Gateway()
    gateway.lost_response = True
    first_model = ScriptedModel(responses=[tool()])
    with pytest.raises(ConnectionError):
        await Assistant(first_model, gateway, history).handle("owner", "Задача", operation_id="same", **TIME)
    model = ScriptedModel(responses=[AIMessage("Задача создана.")])
    reply = await Assistant(model, gateway, history).handle("owner", "Задача", operation_id="same", **TIME)
    assert reply.tool_calls[0].ok
    assert len(gateway.results) == 1
    assert gateway.calls[0][3] == gateway.calls[1][3]
    assert len(model.seen) == 1  # No new model plan before replaying the pending call.


async def test_temporary_storage_error_preserves_call_for_explicit_retry():
    history, gateway = InMemoryChatHistory(), Gateway()
    gateway.temporary = True
    agent = Assistant(ScriptedModel(responses=[tool()]), gateway, history)
    with pytest.raises(TemporaryToolError):
        await agent.handle("owner", "Задача", operation_id="same", **TIME)
    gateway.temporary = False
    reply = await Assistant(ScriptedModel(responses=[AIMessage("Создана задача.")]), gateway, history).handle(
        "owner",
        "Задача",
        operation_id="same",
        **TIME,
    )
    assert reply.tool_calls[0].ok
    assert gateway.calls[0][3] == gateway.calls[1][3]


async def test_repeat_generated_create_call_does_not_create_another_record():
    gateway = Gateway()
    model = ScriptedModel(responses=[tool(), tool(call_id="call-2"), AIMessage("Записал задачу.")])
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle("owner", "Задача", **TIME)
    assert len(reply.tool_calls) == 2
    assert len(gateway.calls) == 1


async def test_verbal_time_range_reaches_create_without_unnecessary_clarification():
    gateway = Gateway()
    model = ScriptedModel(
        responses=[
            tool(
                arguments={
                    "event_type": "meeting",
                    "name": "Хакатон",
                    "datetime": "2026-10-24T13:00:00+03:00",
                    "end_time": "2026-10-24T17:00:00+03:00",
                }
            ),
            AIMessage("Записал встречу завтра с 13:00 до 17:00."),
        ]
    )
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle(
        "owner", "Завтра встреча с часу до пяти по обсуждению хакатона", **TIME
    )
    assert reply.tool_calls[0].ok
    assert len(gateway.calls) == 1
    assert "13:00" in reply.text and not reply.text.endswith("?")


@pytest.mark.parametrize(
    "event_type,text",
    [
        ("meeting", "Завтра встреча по обсуждению хакатона, 10 утра"),
        ("reminder", "Напомни завтра 10 утра позвонить врачу"),
    ],
)
async def test_hour_with_day_part_inside_full_message_is_not_rejected(event_type, text):
    gateway = Gateway()
    arguments = {"event_type": event_type, "name": "Обсуждение", "datetime": "2026-10-24T10:00:00+03:00"}
    model = ScriptedModel(responses=[tool(arguments=arguments), AIMessage("Записал на завтра в 10:00.")])
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle("owner", text, **TIME)
    assert len(gateway.calls) == 1
    assert gateway.calls[0][2] == arguments
    assert reply.tool_calls[0].ok
    assert reply.text == "Записал на завтра в 10:00."
    assert not reply.retried


async def test_reschedule_uses_stable_action_and_accepts_confirmed_update():
    gateway, history = Gateway(), InMemoryChatHistory()
    args = {"meeting_id": "57a58c04-2591-4547-b16b-47bb263be833", "datetime": "2026-10-24T15:00:00+03:00"}
    model = ScriptedModel(
        responses=[tool("reschedule_meeting", args), AIMessage("Перенёс встречу на завтра в 15:00.")]
    )
    agent = Assistant(model, gateway, history)
    reply = await agent.handle(
        "owner", "Перенеси встречу на завтра в три часа дня", operation_id="move", **TIME
    )
    assert reply.tool_calls[0].ok and not reply.retried
    again = await agent.handle(
        "owner", "Перенеси встречу на завтра в три часа дня", operation_id="move", **TIME
    )
    assert again.text == reply.text and len(gateway.calls) == 1


@pytest.mark.parametrize(
    "text", ["Перенёс встречу на завтра.", "Встреча перенесена.", "Встречи перенесены.", "Время изменено."]
)
async def test_invented_reschedule_success_is_not_reported(text):
    model = ScriptedModel(responses=[AIMessage(text)])
    reply = await Assistant(model, Gateway(), InMemoryChatHistory()).handle(
        "owner", "Перенеси встречу", **TIME
    )
    assert reply.retried and "не подтверждено" in reply.text


@pytest.mark.parametrize(
    "name,answer", [("update_event", "Переименовал встречу."), ("delete_event", "Удалил встречу.")]
)
async def test_edit_and_delete_are_confirmed_only_after_their_result(name, answer):
    gateway = Gateway()
    args = {"event_id": "57a58c04-2591-4547-b16b-47bb263be833"}
    if name == "update_event":
        args["name"] = "Новое название"
    model = ScriptedModel(responses=[tool(name, args), tool(name, args, call_id="repeat"), AIMessage(answer)])
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle("owner", "Измени запись", **TIME)
    assert reply.text == answer and not reply.retried
    assert len(gateway.calls) == 1


@pytest.mark.parametrize("text", ["Переименовал встречу.", "Запись удалена.", "Отменил встречу."])
async def test_invented_rename_or_deletion_is_rejected(text):
    model = ScriptedModel(responses=[AIMessage(text)])
    reply = await Assistant(model, Gateway(), InMemoryChatHistory()).handle("owner", "Измени запись", **TIME)
    assert reply.retried and "не подтверждено" in reply.text


async def test_step_limit_after_deletion_reports_completed_action_without_claiming_a_saved_record():
    model = ScriptedModel(
        responses=[tool("delete_event", {"event_id": "57a58c04-2591-4547-b16b-47bb263be833"})]
    )
    reply = await Assistant(model, Gateway(), InMemoryChatHistory(), max_steps=1).handle(
        "owner", "Удали запись", **TIME
    )
    assert reply.tool_calls[0].ok
    assert reply.text.startswith("Действие выполнено") and "сохранена" not in reply.text


async def test_model_cannot_choose_owner_or_call_unapproved_tools():
    gateway = Gateway()
    model = ScriptedModel(
        responses=[
            tool(arguments={"event_type": "task", "name": "Чужая", "owner_id": "other"}),
            tool("run_shell", {"command": "anything"}, call_id="call-2"),
            AIMessage("Не могу выполнить это действие."),
        ]
    )
    reply = await Assistant(model, gateway, InMemoryChatHistory()).handle("owner", "Запрос", **TIME)
    assert all(not call.ok for call in reply.tool_calls)
    assert gateway.calls == []


async def test_unconfirmed_success_is_retried_once_then_replaced():
    model = ScriptedModel(responses=[AIMessage("Готово, записал.")])
    reply = await Assistant(model, Gateway(), InMemoryChatHistory()).handle("owner", "Задача", **TIME)
    assert reply.retried
    assert "не подтверждено" in reply.text
    assert len(model.seen) == 2


async def test_clarification_question_is_not_mistaken_for_confirmation():
    model = ScriptedModel(responses=[AIMessage("На какое время записать встречу?")])
    reply = await Assistant(model, Gateway(), InMemoryChatHistory()).handle("owner", "Встреча", **TIME)
    assert not reply.retried
    assert reply.text.endswith("?")


@pytest.mark.parametrize("event_type", ["reminder", "meeting"])
async def test_model_cannot_invent_a_missing_time(event_type):
    gateway, history = Gateway(), InMemoryChatHistory()
    model = ScriptedModel(
        responses=[
            tool(
                arguments={
                    "event_type": event_type,
                    "name": "Поликлиника",
                    "datetime": "2026-10-24T09:00:00+03:00",
                }
            ),
            AIMessage("Создано."),
        ]
    )
    agent = Assistant(model, gateway, history)
    reply = await agent.handle("owner", "Напомни завтра сходить в поликлинику", **TIME)
    assert gateway.calls == []
    assert reply.text.endswith("?")
    assert reply.tool_calls[0].result["error"]["field"] == "datetime"
    answered = await agent.handle("owner", "В 9", **TIME)
    assert len(gateway.calls) == 1
    assert answered.tool_calls[0].ok


async def test_history_is_scoped_to_owner_and_chat_and_keeps_clarification():
    history = InMemoryChatHistory()
    model = ScriptedModel(responses=[AIMessage("Во сколько встреча?"), AIMessage("Спасибо за уточнение.")])
    agent = Assistant(model, Gateway(), history)
    await agent.handle("owner", "Встреча", chat_id="one", **TIME)
    await agent.handle("owner", "В 16", chat_id="one", **TIME)
    assert any(m.content == "Во сколько встреча?" for m in model.seen[-1])
    await agent.handle("other", "Привет", chat_id="one", **TIME)
    assert not any(m.content == "Во сколько встреча?" for m in model.seen[-1])
    await agent.handle("owner", "Привет", chat_id="two", **TIME)
    assert not any(m.content == "Во сколько встреча?" for m in model.seen[-1])
    await agent.reset("owner", chat_id="one")
    assert await history.load("owner", "one") == []


async def test_step_limit_and_model_timeout():
    gateway = Gateway()
    model = ScriptedModel(
        responses=[tool("get_event_by_query", {"query": "a"}, call_id=f"call-{n}") for n in range(5)]
    )
    reply = await Assistant(model, gateway, InMemoryChatHistory(), max_steps=2).handle(
        "owner", "Найди", **TIME
    )
    assert len(gateway.calls) == 2
    assert "число шагов" in reply.text
    slow = ScriptedModel(responses=[AIMessage("Ответ")], sleep=0.1)
    with pytest.raises(TimeoutError):
        await Assistant(slow, gateway, InMemoryChatHistory(), timeout_s=0.01).handle("owner", "Текст", **TIME)


async def test_one_turn_per_dialog():
    history = InMemoryChatHistory()
    async with history.lock("owner", "private"):
        with pytest.raises(ConversationBusyError):
            await Assistant(ScriptedModel(responses=[AIMessage("Ответ")]), Gateway(), history).handle(
                "owner",
                "Текст",
                **TIME,
            )


@pytest.mark.parametrize(
    "batch",
    [
        [
            {"name": "create_event", "args": {"event_type": "task", "name": "x"}, "id": f"c-{n}"}
            for n in range(6)
        ],
        [{"name": "create_event", "args": {"event_type": "task", "name": "x"}, "id": "duplicate"}] * 2,
    ],
)
async def test_invalid_batch_is_rejected_before_side_effects(batch):
    gateway = Gateway()
    model = ScriptedModel(responses=[AIMessage("", tool_calls=batch)])
    with pytest.raises(ValueError, match="batch"):
        await Assistant(model, gateway, InMemoryChatHistory()).handle("owner", "Текст", **TIME)
    assert gateway.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"user_id": " "},
        {"text": " "},
        {"reference_time": NOW.replace(tzinfo=None)},
        {"timezone": "wrong-zone"},
        {"operation_id": ""},
    ],
)
async def test_invalid_context_is_rejected(change):
    values = {"user_id": "owner", "text": "Текст", **TIME, **change}
    with pytest.raises(ValueError):
        await Assistant(
            ScriptedModel(responses=[AIMessage("Ответ")]), Gateway(), InMemoryChatHistory()
        ).handle(
            **values,
        )


def test_gateway_settings_and_secret_repr():
    secret = "s" * 32
    assert secret not in repr(McpGateway("http://localhost:8001/mcp", secret))
    for url, token in [
        ("http://example.com/mcp", secret),
        ("http://localhost/mcp", "short"),
        ("https://user:password@example.com/mcp", secret),
    ]:
        with pytest.raises(ValueError):
            McpGateway(url, token)


def test_nullable_schema_conversion_preserves_required_fields_and_original_schema():
    original = [
        {
            "name": "example",
            "parameters": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "date": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None},
                },
            },
        }
    ]
    converted = model_tool_schemas(original)[0]["parameters"]
    assert converted["required"] == ["name"]
    assert converted["properties"]["date"] == {"type": "string"}
    assert "anyOf" in original[0]["parameters"]["properties"]["date"]
    with pytest.raises(ValueError, match="nullable"):
        model_tool_schemas([{"parameters": {"anyOf": [{"type": "string"}, {"type": "integer"}]}}])
