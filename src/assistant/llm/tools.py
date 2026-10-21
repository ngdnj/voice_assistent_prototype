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


def _without_nullable_unions(prop: dict[str, Any]) -> dict[str, Any]:
    """`anyOf: [X, null]` -> `X`. GigaChat function calling rejects unions; the field stays optional."""
    variants = prop.get("anyOf")
    if not isinstance(variants, list):
        return prop
    non_null = [v for v in variants if v.get("type") != "null"]
    if len(non_null) != 1 or len(non_null) == len(variants):
        return prop
    simplified = {**{k: v for k, v in prop.items() if k not in ("anyOf", "default")}, **non_null[0]}
    return simplified


def _llm_schema(schema: dict[str, Any]) -> dict[str, Any]:
    stripped = copy.deepcopy(schema)
    properties = stripped.get("properties", {})
    properties.pop(USER_ID_PARAM, None)
    for name, prop in properties.items():
        properties[name] = _without_nullable_unions(prop)
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
        args_schema=_llm_schema(tool.input_schema),
        coroutine=call,
    )


async def load_user_tools(client: Client[Any], user_id: str) -> list[BaseTool]:
    return [_make_tool(client, tool, user_id) for tool in await client.list_tools()]
