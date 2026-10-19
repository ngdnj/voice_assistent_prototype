"""Expose MCP tools to LangChain with `user_id` bound by the system.

The LLM never sees or fills `user_id`: it is removed from the tool schema and
injected on every call, so a prompt cannot make the agent read another
user's records.
"""

import copy
import json
from typing import Any

from fastmcp import Client
from langchain_core.tools import BaseTool, StructuredTool
from mcp.types import TextContent, Tool

USER_ID_PARAM = "user_id"


def _schema_without_user_id(schema: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(schema)
    stripped.get("properties", {}).pop(USER_ID_PARAM, None)
    if USER_ID_PARAM in stripped.get("required", []):
        stripped["required"] = [p for p in stripped["required"] if p != USER_ID_PARAM]
    return stripped


def _result_text(content: list[Any]) -> str:
    return "\n".join(block.text for block in content if isinstance(block, TextContent))


def _make_tool(client: Client[Any], tool: Tool, user_id: str) -> BaseTool:
    async def call(**kwargs: Any) -> str:
        result = await client.call_tool(tool.name, {**kwargs, USER_ID_PARAM: user_id}, raise_on_error=False)
        text = _result_text(result.content)
        # Errors go back to the model as data so it can ask the user a clarifying question.
        return json.dumps({"error": text}, ensure_ascii=False) if result.is_error else text

    return StructuredTool(
        name=tool.name,
        description=tool.description or "",
        args_schema=_schema_without_user_id(tool.input_schema),
        coroutine=call,
    )


async def load_user_tools(client: Client[Any], user_id: str) -> list[BaseTool]:
    return [_make_tool(client, tool, user_id) for tool in await client.list_tools()]
