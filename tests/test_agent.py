from fastmcp import Client
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from assistant.events.storage import EventStorage
from assistant.llm.agent import Assistant, Pricing
from assistant.llm.history import InMemoryChatHistory
from assistant.llm.prompts import render_system_prompt
from tests.conftest import NOW, TZ, scripted_model


def test_system_prompt_contains_iso_time_and_weekday() -> None:
    prompt = render_system_prompt(NOW)
    assert "2026-10-19T12:00:00+03:00" in prompt
    assert "Сегодня: понедельник" in prompt
    assert "2026-10-23 — пятница" in prompt


async def test_agent_creates_event_and_keeps_context(mcp_client: Client, storage: EventStorage) -> None:
    model = scripted_model(
        [
            AIMessage("На какое время поставить встречу?"),
            AIMessage(
                "",
                tool_calls=[
                    {
                        "id": "call-1",
                        "name": "create_event",
                        "args": {
                            "event_type": "meeting",
                            "name": "Встреча с Иваном",
                            "datetime": "2026-10-20T16:00:00+03:00",
                        },
                    }
                ],
                usage_metadata={"input_tokens": 900, "output_tokens": 40, "total_tokens": 940},
            ),
            AIMessage(
                "Готово, встреча с Иваном завтра в 16:00.",
                usage_metadata={"input_tokens": 1000, "output_tokens": 60, "total_tokens": 1060},
            ),
        ]
    )
    history = InMemoryChatHistory(max_turns=10)
    pricing = Pricing(input_rub_per_1k=1.0, output_rub_per_1k=10.0)
    assistant = Assistant(model, mcp_client, history, TZ, pricing)

    first = await assistant.handle("u1", "Поставь встречу с Иваном завтра", now=NOW)
    assert first.tool_calls == []
    assert first.text == "На какое время поставить встречу?"

    second = await assistant.handle("u1", "В 16:00", now=NOW)
    assert [c.name for c in second.tool_calls] == ["create_event"]
    assert second.text == "Готово, встреча с Иваном завтра в 16:00."
    assert second.input_tokens == 1900 and second.output_tokens == 100
    assert second.cost_rub == 1.9 + 1.0

    [event] = storage.search("u1", "Иван")
    assert event.when is not None and event.when.isoformat() == "2026-10-20T16:00:00+03:00"

    messages = await history.load("u1")
    assert isinstance(messages[0], HumanMessage)
    assert any(isinstance(m, ToolMessage) for m in messages)


async def test_history_trims_whole_turns() -> None:
    history = InMemoryChatHistory(max_turns=2)
    for i in range(3):
        await history.append_turn("u1", [HumanMessage(f"q{i}"), AIMessage(f"a{i}")])
    assert [m.text for m in await history.load("u1")] == ["q1", "a1", "q2", "a2"]
