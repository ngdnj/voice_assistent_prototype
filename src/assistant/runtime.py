"""Wiring of the assistant from settings: model, MCP client and dialog history."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastmcp import Client
from redis.asyncio import Redis

from assistant.config import Settings
from assistant.events.storage import EventStorage
from assistant.llm.agent import Assistant
from assistant.llm.history import ChatHistory, InMemoryChatHistory, RedisChatHistory
from assistant.llm.model import build_chat_model
from assistant.mcp_server.server import build_server


def _mcp_client(settings: Settings) -> Client[Any]:
    if settings.mcp_url:
        return Client(settings.mcp_url)
    # No external MCP server configured: run it in-process over the in-memory transport.
    storage = EventStorage(settings.storage_path, settings.tz)
    return Client(build_server(storage, settings.tz))


@asynccontextmanager
async def open_assistant(settings: Settings) -> AsyncIterator[Assistant]:
    model = build_chat_model(settings)
    redis: Redis | None = None
    history: ChatHistory
    if settings.redis_url:
        redis = Redis.from_url(settings.redis_url, decode_responses=True)
        await redis.ping()  # fail fast instead of silently losing dialog context
        history = RedisChatHistory(redis, settings.history_max_turns, settings.history_ttl_s)
    else:
        history = InMemoryChatHistory(settings.history_max_turns)
    try:
        async with _mcp_client(settings) as client:
            yield Assistant(model, client, history, settings.tz, settings.llm_price_rub_per_1k_tokens)
    finally:
        if redis is not None:
            await redis.aclose()
