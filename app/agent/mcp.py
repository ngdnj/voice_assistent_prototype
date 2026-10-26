"""MCP HTTP boundary: trusted context is supplied by code, never by the model."""

from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import urlsplit

from fastmcp import Client
from fastmcp.client.client import CallToolResult
from fastmcp.client.transports import StreamableHttpTransport

TOOLS = frozenset(
    {
        "create_event",
        "get_events_by_period",
        "get_event_by_query",
        "reschedule_meeting",
        "update_event",
        "delete_event",
    }
)


@dataclass(frozen=True)
class Caller:
    owner_id: str
    timezone: str
    request_id: str


class ToolGateway(Protocol):
    async def list_tools(self, caller: Caller) -> list[dict[str, Any]]: ...

    async def call(
        self, caller: Caller, name: str, arguments: dict[str, Any], *, action_id: str
    ) -> dict[str, Any]: ...


@dataclass(frozen=True)
class McpGateway:
    url: str
    service_token: str = field(repr=False)
    timeout: float = 15.0

    def __post_init__(self) -> None:
        url = urlsplit(self.url)
        if url.scheme not in ("http", "https") or not url.hostname or url.username or url.password:
            raise ValueError("MCP_URL must be an HTTP URL without credentials")
        if url.scheme == "http" and url.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("Remote MCP requires HTTPS")
        if len(self.service_token) < 32:
            raise ValueError("MCP_SERVICE_TOKEN must contain at least 32 characters")
        if not 0 < self.timeout <= 60:
            raise ValueError("MCP timeout must be between 0 and 60 seconds")

    def _client(self, caller: Caller, action_id: str = "") -> Client[StreamableHttpTransport]:
        headers = {
            "Authorization": f"Bearer {self.service_token}",
            "X-Owner-Id": caller.owner_id,
            "X-Timezone": caller.timezone,
            "X-Request-Id": caller.request_id,
        }
        if action_id:
            headers["X-Action-Id"] = action_id
        return Client(StreamableHttpTransport(self.url, headers=headers), timeout=self.timeout)

    async def list_tools(self, caller: Caller) -> list[dict[str, Any]]:
        async with self._client(caller) as client:
            tools = await client.list_tools()
        selected = [
            {"name": tool.name, "description": tool.description or "", "parameters": tool.input_schema}
            for tool in tools
            if tool.name in TOOLS
        ]
        if {tool["name"] for tool in selected} != TOOLS:
            raise ValueError("MCP server does not expose the agreed tools")
        return selected

    async def call(
        self, caller: Caller, name: str, arguments: dict[str, Any], *, action_id: str
    ) -> dict[str, Any]:
        if name not in TOOLS:
            return {"error": {"code": "validation_error", "message": "Инструмент недоступен", "field": None}}
        if set(arguments) & {"owner_id", "user_id", "request_id", "action_id", "operation_id"}:
            return {"error": {"code": "validation_error", "message": "Недопустимый контекст", "field": None}}
        async with self._client(caller, action_id) as client:
            result: CallToolResult = await client.call_tool(name, arguments, raise_on_error=False)
        if result.is_error or result.structured_content is None:
            raise RuntimeError("MCP returned an invalid tool result")
        return result.structured_content
