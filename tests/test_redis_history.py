"""Integration test against a real Redis/Valkey (`docker compose up -d redis`)."""

import os
import uuid
from collections.abc import AsyncIterator

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from redis.asyncio import Redis
from redis.exceptions import ConnectionError as RedisConnectionError

from assistant.llm.history import RedisChatHistory

REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/15")


@pytest.fixture
async def redis() -> AsyncIterator[Redis]:
    client = Redis.from_url(REDIS_URL, decode_responses=True)
    try:
        await client.ping()
    except RedisConnectionError:
        await client.aclose()
        pytest.skip(f"Redis is not available at {REDIS_URL}")
    yield client
    await client.aclose()


async def test_round_trip_trim_and_ttl(redis: Redis) -> None:
    user = f"test-{uuid.uuid4().hex}"
    history = RedisChatHistory(redis, max_turns=2, ttl_s=60)
    tool_call = {"id": "c1", "name": "get_event", "args": {"event_type": []}}
    for i in range(3):
        await history.append_turn(
            user,
            [
                HumanMessage(f"q{i}"),
                AIMessage("", tool_calls=[tool_call]),
                ToolMessage('{"count": 0}', tool_call_id="c1"),
                AIMessage(f"a{i}"),
            ],
        )
    try:
        messages = await history.load(user)
        assert [m.text for m in messages if isinstance(m, HumanMessage)] == ["q1", "q2"]
        assert isinstance(messages[1], AIMessage) and messages[1].tool_calls[0]["name"] == "get_event"
        assert 0 < await redis.ttl(f"assistant:history:{user}") <= 60
    finally:
        await history.clear(user)
    assert await history.load(user) == []
