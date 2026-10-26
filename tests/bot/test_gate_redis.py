import os
from collections.abc import AsyncIterator
from uuid import uuid4

import pytest

from app.bot.gate import EVENT_TTL_S, RedisGate
from app.storage.redis import RedisSettings, redis_connection

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REDIS_INTEGRATION") != "1",
    reason="set RUN_REDIS_INTEGRATION=1 for a dedicated test Redis",
)


@pytest.fixture
async def gate() -> AsyncIterator[RedisGate]:
    prefix = f"it-gorky:test:{uuid4()}"
    async with redis_connection(RedisSettings.from_env()) as client:
        yield RedisGate(client, prefix=prefix, busy_ttl_s=180)
        # Remove only our own keys; never FLUSHDB.
        keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
        if keys:
            await client.delete(*keys)


async def test_lock_has_owner_and_ttl(gate: RedisGate) -> None:
    assert await gate.acquire("42", "job-1")
    assert not await gate.acquire("42", "job-2")
    assert await gate.is_busy("42")
    assert not await gate.is_busy("43")
    ttl = await gate._client.ttl(gate.busy_key("42"))
    assert 0 < ttl <= 180
    assert not await gate.release("42", "job-2")  # Not the owner.
    assert await gate.is_busy("42")
    assert await gate.release("42", "job-1")
    assert not await gate.is_busy("42")
    assert await gate.acquire("42", "job-3")


async def test_event_deduplication(gate: RedisGate) -> None:
    assert await gate.first_seen("max:message:mid.1")
    assert not await gate.first_seen("max:message:mid.1")
    ttl = await gate._client.ttl(gate.event_key("max:message:mid.1"))
    assert 0 < ttl <= EVENT_TTL_S
    await gate.forget("max:message:mid.1")
    assert await gate.first_seen("max:message:mid.1")


async def test_keys_encode_user_ids(gate: RedisGate) -> None:
    assert gate.busy_key("42").endswith(":v1:bot:busy:3432")
    assert ":" not in gate.event_key("a:b").rsplit(":event:", 1)[1]
