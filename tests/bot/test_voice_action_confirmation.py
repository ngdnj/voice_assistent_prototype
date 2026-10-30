"""Voice confirmation executes the persisted agent plan, without a second question."""

import json
from uuid import UUID

import pytest
from langchain_core.messages import AIMessage

from app.agent.agent import Assistant
from app.agent.bot_backend import AgentBackend
from app.agent.history import InMemoryChatHistory
from app.bot.service import BackendRequest
from app.bot.updates import CallbackPressed
from tests.agent_support import ScriptedModel, tool
from tests.bot.fakes import NOW, Collect, FakeGate, FakeRecognizer, FakeSender, build_bot
from tests.bot.test_service import VOICE
from tests.test_agent import Gateway
from tests.test_agent_security import RECORD


class RecordsGateway(Gateway):
    def __init__(self, kind="meeting", count=1):
        super().__init__()
        self.records = [
            {**RECORD, "id": str(UUID(int=i + 1)), "name": f"Запись {i + 1}", "event_type": kind}
            for i in range(count)
        ]

    async def call(self, caller, name, arguments, *, action_id):
        await super().call(caller, name, arguments, action_id=action_id)
        if name.startswith("get_"):
            return {"events": self.records}
        record = next(
            r for r in self.records if r["id"] == arguments.get("event_id", arguments.get("meeting_id"))
        )
        return {"deleted" if name == "delete_event" else "updated": {**record, **arguments}}


class CaptureBackend(AgentBackend):
    def __init__(self, assistant):
        super().__init__(assistant)
        self.requests = []

    async def answer(self, request: BackendRequest):
        self.requests.append(request)
        return await super().answer(request)


def scenario(kind, name, count=1, text=None, single_call=False):
    gateway = RecordsGateway(kind, count)
    args = [
        {"event_id": r["id"], **({"name": "Новое название"} if name == "update_event" else {})}
        for r in gateway.records
    ]
    if name == "reschedule_meeting":
        args = [{"meeting_id": r["id"], "datetime": "2026-10-26T15:00:00+03:00"} for r in gateway.records]
    calls = AIMessage(
        "",
        tool_calls=[
            {"name": name, "args": a, "id": f"change-{i}"}
            for i, a in enumerate(args[:1] if single_call else args)
        ],
    )
    model = ScriptedModel(responses=[tool("get_event_by_query", {"query": "запись"}), calls])
    history = InMemoryChatHistory()
    backend = CaptureBackend(Assistant(model, gateway, history))
    sender, gate, submit = FakeSender(), FakeGate(), Collect()
    text = text or ("Удали" if name == "delete_event" else "Переименуй") + " запись 1"
    bot, _ = build_bot(
        backend=backend, sender=sender, gate=gate, submit=submit, recognizer=FakeRecognizer(text)
    )
    return bot, backend, gateway, model, sender, gate, submit


def press(gate, yes=True, **kwargs):
    key = json.loads(gate.voices["42"])["id"]
    return CallbackPressed(
        callback_id="cb-action",
        payload=f"voice:{'yes' if yes else 'no'}:{key}",
        owner_id="42",
        chat_id=kwargs.get("chat_id", "777"),
        message_id="confirmation",
    )


@pytest.mark.parametrize("kind", ["task", "meeting", "reminder"])
@pytest.mark.parametrize("name", ["delete_event", "update_event"])
async def test_one_button_executes_exact_plan_for_each_kind(kind, name):
    bot, backend, gateway, model, sender, gate, submit = scenario(kind, name)
    await bot.handle(VOICE)
    await submit.drain()
    assert [c[1] for c in gateway.calls] == ["get_event_by_query"]
    assert "Распознано:" in sender.texts[-1]
    assert "Запись 1" in sender.texts[-1]
    assert sender.buttons[-1]
    before = len(model.seen)
    callback = press(gate)
    assert await bot.handle(callback) == "accepted"
    await submit.drain()
    assert len(model.seen) == before
    assert [c[1] for c in gateway.calls] == ["get_event_by_query", name]
    assert sender.texts[-1].startswith("Удалено:" if name == "delete_event" else "Изменено:")
    confirmed = backend.requests[-1]
    assert confirmed.text == "да" and confirmed.confirmation_operation_id == backend.requests[0].job_id
    assert confirmed.reference_time == NOW
    await backend.answer(confirmed)
    assert len(gateway.calls) == 2
    assert await bot.handle(callback) == "duplicate"


@pytest.mark.parametrize("name", ["delete_event", "update_event"])
@pytest.mark.parametrize("single_call", [True, False])
async def test_one_confirmation_covers_all_explicitly_selected_records(name, single_call):
    bot, backend, gateway, model, sender, gate, submit = scenario(
        "meeting",
        name,
        2,
        text=("Удали" if name == "delete_event" else "Переименуй") + " все встречи завтра",
        single_call=single_call,
    )
    await bot.handle(VOICE)
    await submit.drain()
    assert len(gateway.calls) == 1
    assert "Запись 1" in sender.texts[-1] and "Запись 2" in sender.texts[-1]
    before = len(model.seen)
    await bot.handle(press(gate))
    await submit.drain()
    assert len(model.seen) == before
    assert [c[1] for c in gateway.calls] == ["get_event_by_query", name, name]
    assert len({c[3] for c in gateway.calls[1:]}) == 2
    await backend.answer(backend.requests[-1])
    assert len(gateway.calls) == 3


async def test_reschedule_executes_after_one_voice_button():
    bot, _backend, gateway, model, sender, gate, submit = scenario(
        "meeting", "reschedule_meeting", text="Перенеси запись 1 на завтра в 15:00"
    )
    await bot.handle(VOICE)
    await submit.drain()
    assert len(gateway.calls) == 1
    before = len(model.seen)
    await bot.handle(press(gate))
    await submit.drain()
    assert len(model.seen) == before
    assert gateway.calls[-1][1] == "reschedule_meeting"
    assert "15:00" in gateway.calls[-1][2]["datetime"]
    assert sender.texts[-1].startswith("Изменено:")


async def test_word_all_in_a_record_title_cannot_expand_deletion():
    bot, _backend, gateway, _model, _sender, gate, submit = scenario(
        "meeting", "delete_event", 2, text="Удали встречу «Купить все товары»", single_call=True
    )
    gateway.records[0]["name"] = "Купить все товары"
    await bot.handle(VOICE)
    await submit.drain()
    await bot.handle(press(gate))
    await submit.drain()
    assert [c[2]["event_id"] for c in gateway.calls if c[1] == "delete_event"] == [gateway.records[0]["id"]]


@pytest.mark.parametrize("interruption", ["cancel", "new_request", "other_chat"])
async def test_invalid_or_cancelled_button_cannot_execute_a_change(interruption):
    bot, backend, gateway, _model, _sender, gate, submit = scenario("meeting", "delete_event")
    await bot.handle(VOICE)
    await submit.drain()
    callback = press(
        gate, yes=interruption != "cancel", chat_id="999" if interruption == "other_chat" else "777"
    )
    if interruption == "new_request":
        await backend.assistant.handle(
            "42", "Что такое bogosort?", reference_time=NOW, timezone="Europe/Moscow", chat_id="777"
        )
    await bot.handle(callback)
    await submit.drain()
    assert [c[1] for c in gateway.calls] == ["get_event_by_query"]
    if interruption == "cancel":
        reply = await backend.assistant.handle(
            "42", "да", reference_time=NOW, timezone="Europe/Moscow", chat_id="777"
        )
        assert "Нет действующего" in reply.text
        assert len(gateway.calls) == 1


async def test_ambiguous_voice_asks_for_selection_before_any_confirmation():
    bot, _backend, gateway, _model, sender, gate, submit = scenario(
        "meeting", "delete_event", 2, text="Удали встречу"
    )
    await bot.handle(VOICE)
    await submit.drain()
    assert "Какую запись удалить?" in sender.texts[-1]
    assert not gate.voices
    assert len(gateway.calls) == 1


async def test_mixed_creation_and_deletion_cannot_write_before_confirmation():
    bot, backend, gateway, _model, sender, gate, submit = scenario(
        "meeting", "delete_event", text="Создай задачу и удали встречу"
    )
    await bot.handle(VOICE)
    await submit.drain()
    assert not backend.requests and not gateway.calls and not gate.voices
    assert "отдельными запросами" in sender.texts[-1]
