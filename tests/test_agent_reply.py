"""The typed result the agent runner (role 1) gets: tool calls with their MCP results."""

from fastmcp import Client
from langchain_core.messages import AIMessage

from assistant.llm.agent import Assistant, Pricing
from assistant.llm.history import InMemoryChatHistory
from tests.conftest import NOW, TZ_NAME, scripted_model


async def test_reply_carries_tool_results(mcp_client: Client) -> None:
    meeting = {"event_type": "meeting", "name": "Встреча с Иваном", "datetime": "2026-10-20T16:00:00+03:00"}
    create = {"id": "call-1", "name": "create_event", "args": meeting}
    broken = {"id": "call-2", "name": "create_event", "args": {"event_type": "meeting", "name": "Встреча"}}
    model = scripted_model(
        [AIMessage("", tool_calls=[create, broken]), AIMessage("Записал встречу с Иваном на завтра 16:00.")]
    )
    assistant = Assistant(model, mcp_client, InMemoryChatHistory(max_turns=10), Pricing(0, 0))

    reply = await assistant.handle("u1", "Встреча с Иваном завтра в 16", reference_time=NOW, timezone=TZ_NAME)

    ok, failed = reply.tool_calls
    assert ok.ok
    assert ok.result is not None and ok.result["created"]["name"] == "Встреча с Иваном"
    assert not failed.ok
    assert failed.result is not None and "error" in failed.result
