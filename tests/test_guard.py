import pytest
from fastmcp import Client
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from assistant.events.models import EventType
from assistant.events.storage import EventStorage
from assistant.llm import guard
from assistant.llm.agent import Assistant, Pricing
from assistant.llm.history import InMemoryChatHistory
from tests.conftest import NOW, TZ_NAME, scripted_model

CREATE_MEETING = AIMessage(
    "",
    tool_calls=[
        {
            "id": "call-1",
            "name": "create_event",
            "args": {
                "event_type": "meeting",
                "name": "Встреча с дизайнером",
                "datetime": "2026-10-22T11:00:00+03:00",
            },
        }
    ],
)


@pytest.mark.parametrize(
    "text",
    [
        "Встреча с дизайнером в четверг в 11:00 — записал.",
        "Задача «Отправить отчёт» создана на пятницу.",
        "Добавила напоминание на 10:00.",
        "Встреча сохранена.",
        "Созвон назначен на понедельник.",
    ],
)
def test_claims_saved(text: str) -> None:
    assert guard.claims_saved(text)


@pytest.mark.parametrize(
    "text",
    [
        "Во сколько поставить встречу с Иваном?",
        "Сегодня у вас запланировано две записи.",
        "Записать встречу на 11:00?",
        "Привет! Я веду ваши задачи и встречи.",
    ],
)
def test_does_not_flag_questions_and_plans(text: str) -> None:
    assert not guard.claims_saved(text)


def test_listing_existing_records_is_not_a_false_claim() -> None:
    turn = [
        AIMessage("", tool_calls=[{"id": "c", "name": "get_event", "args": {}}]),
        ToolMessage('{"count": 1}', tool_call_id="c", name="get_event"),
        AIMessage("Сегодня у вас созвон, он назначен на 16:00."),
    ]
    assert not guard.is_false_save_claim(turn)


def _assistant(mcp_client: Client, responses: list[AIMessage]) -> tuple[Assistant, InMemoryChatHistory]:
    history = InMemoryChatHistory(max_turns=10)
    pricing = Pricing(input_rub_per_1k=0, output_rub_per_1k=0)
    return Assistant(scripted_model(list(responses)), mcp_client, history, pricing), history


async def test_false_claim_is_retried_and_record_created(mcp_client: Client, storage: EventStorage) -> None:
    assistant, history = _assistant(
        mcp_client,
        [
            AIMessage("Встреча с дизайнером в четверг в 11:00 — записал."),  # lie, no tool call
            CREATE_MEETING,
            AIMessage("Встреча с дизайнером записана на четверг, 22 октября, в 11:00."),
        ],
    )
    reply = await assistant.handle(
        "u1", "В четверг в 11 у меня встреча с дизайнером", reference_time=NOW, timezone=TZ_NAME
    )

    assert reply.retried
    assert [c.name for c in reply.tool_calls] == ["create_event"]
    assert reply.text == "Встреча с дизайнером записана на четверг, 22 октября, в 11:00."
    assert len(storage.search("u1", "дизайнер")) == 1
    texts = [m.text for m in await history.load("u1")]
    assert "Встреча с дизайнером в четверг в 11:00 — записал." not in texts
    assert guard.RETRY_INSTRUCTION not in texts


async def test_repeated_false_claim_is_replaced_with_honest_answer(
    mcp_client: Client, storage: EventStorage
) -> None:
    assistant, history = _assistant(
        mcp_client,
        [AIMessage("Записал встречу."), AIMessage("Готово, встреча сохранена.")],
    )
    reply = await assistant.handle(
        "u1", "В четверг в 11 у меня встреча с дизайнером", reference_time=NOW, timezone=TZ_NAME
    )

    assert reply.retried
    assert reply.text == guard.NOT_SAVED_ANSWER
    assert storage.search("u1", "встреча") == []
    messages = await history.load("u1")
    assert isinstance(messages[0], HumanMessage)
    assert messages[-1].text == guard.NOT_SAVED_ANSWER


async def test_failed_create_then_claim_is_caught(mcp_client: Client, storage: EventStorage) -> None:
    no_time = AIMessage(
        "",
        tool_calls=[
            {"id": "c", "name": "create_event", "args": {"event_type": "meeting", "name": "Встреча"}}
        ],
    )
    assistant, _ = _assistant(
        mcp_client,
        [no_time, AIMessage("Встреча добавлена."), AIMessage("Во сколько будет встреча?")],
    )
    reply = await assistant.handle("u1", "Поставь встречу завтра", reference_time=NOW, timezone=TZ_NAME)

    assert reply.retried
    assert reply.text == "Во сколько будет встреча?"
    assert storage.search("u1", "встреча") == []


def test_existing_record_counts_as_saved() -> None:
    turn = [
        AIMessage("", tool_calls=[{"id": "c", "name": "create_event", "args": {}}]),
        ToolMessage('{"already_exists": {"id": 1}}', tool_call_id="c", name="create_event"),
        AIMessage("Такая задача уже записана."),
    ]
    assert not guard.is_false_save_claim(turn)


@pytest.mark.parametrize("text", ["Удалил встречу с Иваном.", "Встреча отменена.", "Напоминание удалено."])
def test_claims_deleted(text: str) -> None:
    assert guard.claims_deleted(text)


def test_delete_claim_without_delete_event_is_false() -> None:
    search_only = [
        AIMessage("", tool_calls=[{"id": "c", "name": "get_event_by_query", "args": {"text": "Иван"}}]),
        ToolMessage('{"count": 1}', tool_call_id="c", name="get_event_by_query"),
        AIMessage("Встреча с Иваном удалена."),
    ]
    assert guard.false_claim(search_only) is guard.Claim.DELETED
    deleted = [
        *search_only[:2],
        AIMessage("", tool_calls=[{"id": "d", "name": "delete_event", "args": {"event_id": 1}}]),
        ToolMessage('{"deleted": {"id": 1}}', tool_call_id="d", name="delete_event"),
        AIMessage("Удалил встречу с Иваном, которая была назначена на 14:00."),
    ]
    assert guard.false_claim(deleted) is None


async def test_false_delete_claim_is_retried(mcp_client: Client, storage: EventStorage) -> None:
    event = storage.create("u1", EventType.MEETING, "Встреча с Иваном", "", NOW)
    delete_call = AIMessage(
        "", tool_calls=[{"id": "d1", "name": "delete_event", "args": {"event_id": event.id}}]
    )
    assistant, _ = _assistant(
        mcp_client,
        [AIMessage("Встреча с Иваном удалена."), delete_call, AIMessage("Удалил встречу с Иваном.")],
    )
    reply = await assistant.handle("u1", "Удали встречу с Иваном", reference_time=NOW, timezone=TZ_NAME)

    assert reply.retried
    assert [c.name for c in reply.tool_calls] == ["delete_event"]
    assert storage.search("u1", "Иван") == []
