"""Adapt MCP schemas to the selected model's supported function schema subset."""

from typing import Any


def _schema(value: Any) -> Any:
    if isinstance(value, list):
        return [_schema(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: _schema(item) for key, item in value.items() if key != "anyOf"}
    if "anyOf" in value:
        choices = value["anyOf"]
        concrete = [choice for choice in choices if choice.get("type") != "null"]
        if len(concrete) != 1 or len(choices) != 2:
            raise ValueError("Model adapter only supports nullable single-type MCP fields")
        # Optionality remains in the parent's required list. The MCP server still
        # accepts null; the model should omit an absent optional value.
        result = {**_schema(concrete[0]), **result}
        if result.get("default") is None:
            result.pop("default", None)
    return result


def model_tool_schemas(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**tool, "parameters": _schema(tool["parameters"])} for tool in tools]
