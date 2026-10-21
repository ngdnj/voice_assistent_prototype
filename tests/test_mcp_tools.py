import json

from fastmcp import Client

from assistant.events.storage import EventStorage
from assistant.llm.tools import USER_ID_PARAM, load_user_tools
from assistant.mcp_server.server import build_server
from tests.conftest import TZ


async def test_every_tool_and_server_has_description(mcp_client: Client, storage: EventStorage) -> None:
    assert build_server(storage, TZ).instructions
    tools = await mcp_client.list_tools()
    assert {t.name for t in tools} == {"create_event", "get_event", "get_event_by_query", "delete_event"}
    for tool in tools:
        assert tool.description
        for name, prop in tool.input_schema["properties"].items():
            assert prop.get("description"), f"{tool.name}.{name} has no description"


async def test_user_id_is_hidden_from_llm_schema(mcp_client: Client) -> None:
    for tool in await load_user_tools(mcp_client, "u1"):
        schema = tool.args_schema
        assert isinstance(schema, dict)
        assert USER_ID_PARAM not in schema["properties"]
        assert USER_ID_PARAM not in schema.get("required", [])


async def test_tools_are_bound_to_their_user(mcp_client: Client) -> None:
    u1 = {t.name: t for t in await load_user_tools(mcp_client, "u1")}
    u2 = {t.name: t for t in await load_user_tools(mcp_client, "u2")}

    await u1["create_event"].ainvoke({"event_type": "task", "name": "Проверить договор", "datetime": None})

    found_u1 = json.loads(await u1["get_event_by_query"].ainvoke({"text": "договор"}))
    found_u2 = json.loads(await u2["get_event_by_query"].ainvoke({"text": "договор"}))
    assert found_u1["count"] == 1
    assert found_u2["count"] == 0


async def test_meeting_without_time_returns_error_for_llm(mcp_client: Client) -> None:
    tools = {t.name: t for t in await load_user_tools(mcp_client, "u1")}
    result = json.loads(
        await tools["create_event"].ainvoke({"event_type": "meeting", "name": "Встреча с Иваном"})
    )
    assert "время" in result["error"]


async def test_get_event_uses_iso_range(mcp_client: Client) -> None:
    tools = {t.name: t for t in await load_user_tools(mcp_client, "u1")}
    await tools["create_event"].ainvoke(
        {"event_type": "meeting", "name": "Встреча с командой", "datetime": "2026-10-20T15:00:00+03:00"}
    )
    result = json.loads(
        await tools["get_event"].ainvoke(
            {
                "event_type": ["meeting"],
                "start_time": "2026-10-20T00:00:00+03:00",
                "end_time": "2026-10-20T23:59:59+03:00",
            }
        )
    )
    assert result["count"] == 1
    assert result["events"][0]["datetime"] == "2026-10-20T15:00:00+03:00"


async def test_invalid_iso_is_reported(mcp_client: Client) -> None:
    tools = {t.name: t for t in await load_user_tools(mcp_client, "u1")}
    result = json.loads(
        await tools["get_event"].ainvoke(
            {"event_type": [], "start_time": "завтра", "end_time": "2026-10-20T23:59:59+03:00"}
        )
    )
    assert "ISO" in result["error"]


async def test_llm_schema_has_no_unions(mcp_client: Client) -> None:
    """GigaChat function calling rejects anyOf; optional fields must stay plain and not required."""
    for tool in await load_user_tools(mcp_client, "u1"):
        schema = tool.args_schema
        assert isinstance(schema, dict)
        assert "anyOf" not in json.dumps(schema), tool.name
    create = next(t for t in await load_user_tools(mcp_client, "u1") if t.name == "create_event")
    assert isinstance(create.args_schema, dict)
    assert create.args_schema["properties"]["datetime"]["type"] == "string"
    assert "datetime" not in create.args_schema["required"]


async def test_repeated_create_does_not_duplicate(mcp_client: Client, storage: EventStorage) -> None:
    tools = {t.name: t for t in await load_user_tools(mcp_client, "u1")}
    args = {"event_type": "task", "name": "Презентация", "datetime": "2026-11-21T23:59:00+03:00"}
    first = json.loads(await tools["create_event"].ainvoke(args))
    second = json.loads(await tools["create_event"].ainvoke(args))

    assert "created" in first
    assert second["already_exists"]["id"] == first["created"]["id"]
    assert len(storage.search("u1", "презентация")) == 1


async def test_delete_event_is_bound_to_user(mcp_client: Client, storage: EventStorage) -> None:
    u1 = {t.name: t for t in await load_user_tools(mcp_client, "u1")}
    u2 = {t.name: t for t in await load_user_tools(mcp_client, "u2")}
    created = json.loads(
        await u1["create_event"].ainvoke(
            {"event_type": "meeting", "name": "Встреча с Иваном", "datetime": "2026-10-20T15:00:00+03:00"}
        )
    )
    event_id = created["created"]["id"]

    foreign = json.loads(await u2["delete_event"].ainvoke({"event_id": event_id}))
    assert "не найдена" in foreign["error"]
    assert storage.search("u1", "Иван")

    own = json.loads(await u1["delete_event"].ainvoke({"event_id": event_id}))
    assert own["deleted"]["id"] == event_id
    assert storage.search("u1", "Иван") == []
