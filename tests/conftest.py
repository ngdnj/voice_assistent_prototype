from collections.abc import AsyncIterator, Sequence
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import pytest
from fastmcp import Client
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import BaseMessage

from assistant.events.storage import EventStorage
from assistant.mcp_server.server import build_server

TZ = ZoneInfo("Europe/Moscow")
# Monday.
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=TZ)


class ToolCallingFakeModel(FakeMessagesListChatModel):
    """Scripted chat model that accepts tools, for agent tests without an LLM."""

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> "ToolCallingFakeModel":
        return self


def scripted_model(messages: list[BaseMessage]) -> ToolCallingFakeModel:
    return ToolCallingFakeModel(responses=messages)


@pytest.fixture
def storage() -> EventStorage:
    return EventStorage(":memory:", TZ)


@pytest.fixture
async def mcp_client(storage: EventStorage) -> AsyncIterator[Client]:
    async with Client(build_server(storage, TZ)) as client:
        yield client
