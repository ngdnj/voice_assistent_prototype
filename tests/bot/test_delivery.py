import asyncio
import json
import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from app.bot import delivery as delivery_module
from app.bot.client import SendResult
from app.bot.delivery import DeliveryConsumer, stream_entries
from app.storage.notification_stream import NotificationStream, NotificationStreams
from app.storage.redis import RedisSettings, redis_connection
from app.workers.notification import Notification

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
needs_redis = pytest.mark.skipif(
    os.environ.get("RUN_REDIS_INTEGRATION") != "1",
    reason="set RUN_REDIS_INTEGRATION=1 for a dedicated test Redis",
)


class FakeSender:
    def __init__(self, *results: SendResult) -> None:
        self.results = list(results) or [SendResult("delivered", external_message_id="mid.1")]
        self.calls: list[tuple[str, str]] = []
        self.delay = 0.0

    async def send_to_user(self, *, user_id: str, text: str) -> SendResult:
        self.calls.append((user_id, text))
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.results.pop(0) if len(self.results) > 1 else self.results[0]


def notice(owner_id: str = "42", text: str = "🔔 Напоминание: позвонить") -> Notification:
    return Notification(notification_id=uuid4(), owner_id=owner_id, text=text, created_at=NOW)


@pytest.fixture
async def stream() -> AsyncIterator[NotificationStream]:
    keys = NotificationStreams(prefix=f"it-gorky:test:{uuid4()}")
    async with redis_connection(RedisSettings.from_env()) as client:
        value = NotificationStream(client, keys=keys)
        await value.ensure_group()
        try:
            yield value
        finally:
            # Only our own keys; never FLUSHDB.
            await client.delete(keys.incoming, keys.results, keys.dead)


def consumer(stream: NotificationStream, sender: FakeSender, name: str = "test-a") -> DeliveryConsumer:
    return DeliveryConsumer(stream, sender, name=name, clock=lambda: NOW, reclaim_idle_ms=0, block_ms=10)


async def results(stream: NotificationStream) -> list[dict[str, object]]:
    entries = await stream.client.xrange(stream.keys.results)
    return [json.loads(fields["data"]) for _, fields in entries]


async def pending(stream: NotificationStream) -> int:
    return int((await stream.client.xpending(stream.keys.incoming, stream.keys.group))["pending"])


def test_stream_entries_accepts_resp2_and_resp3() -> None:
    entry = ("1-0", {"version": "1"})
    assert stream_entries(None, "s") == []
    assert stream_entries([["s", [entry]]], "s") == [entry]
    assert stream_entries({"s": [[entry]]}, "s") == [entry]
    assert stream_entries({"other": [[entry]]}, "s") == []


@needs_redis
async def test_delivered_to_owner_and_acked(stream: NotificationStream) -> None:
    sender = FakeSender()
    value = notice()
    await stream.publish(value)
    assert await consumer(stream, sender).read_new() == 1
    assert sender.calls == [("42", value.text)]
    [result] = await results(stream)
    assert result["status"] == "delivered" and result["external_message_id"] == "mid.1"
    assert result["notification_id"] == str(value.notification_id)
    assert await pending(stream) == 0


@needs_redis
@pytest.mark.parametrize("owner_id", ["user-42", "0", "-5"])
async def test_owner_without_max_user_is_dead_lettered_without_send(
    stream: NotificationStream, owner_id: str
) -> None:
    sender = FakeSender()
    await stream.publish(notice(owner_id=owner_id))
    await consumer(stream, sender).read_new()
    assert sender.calls == []
    [result] = await results(stream)
    assert (result["status"], result["outcome"], result["error_code"]) == (
        "dead_lettered",
        "permanent_failure",
        "unknown_recipient",
    )
    assert await stream.client.xlen(stream.keys.dead) == 1
    assert await pending(stream) == 0


@needs_redis
async def test_invalid_envelope_is_dead_lettered(stream: NotificationStream) -> None:
    sender = FakeSender()
    await stream.client.xadd(stream.keys.incoming, {"version": "2", "text": "x"})
    await consumer(stream, sender).read_new()
    assert sender.calls == []
    [result] = await results(stream)
    assert (result["status"], result["error_code"]) == ("dead_lettered", "invalid_envelope")
    assert await pending(stream) == 0


@needs_redis
async def test_temporary_failure_is_retried_through_reclaim(stream: NotificationStream) -> None:
    sender = FakeSender(
        SendResult("retryable_failure", error_code="http_503"),
        SendResult("delivered", external_message_id="mid.2"),
    )
    await stream.publish(notice())
    worker = consumer(stream, sender)
    await worker.read_new()
    assert await pending(stream) == 1
    assert await worker.reclaim() == 1
    statuses = [(item["status"], item["delivery_count"]) for item in await results(stream)]
    assert statuses == [("retrying", 1), ("delivered", 2)]
    assert len(sender.calls) == 2
    assert await pending(stream) == 0


@needs_redis
async def test_third_failure_dead_letters_and_no_fourth_send(stream: NotificationStream) -> None:
    sender = FakeSender(SendResult("unknown", error_code="timeout"))
    await stream.publish(notice())
    worker = consumer(stream, sender)
    await worker.read_new()
    await worker.reclaim()
    await worker.reclaim()
    statuses = [(item["status"], item["outcome"]) for item in await results(stream)]
    assert statuses == [("retrying", "unknown"), ("retrying", "unknown"), ("dead_lettered", "unknown")]
    assert len(sender.calls) == 3
    assert await pending(stream) == 0
    assert await worker.reclaim() == 0


@needs_redis
async def test_permanent_failure_is_not_retried(stream: NotificationStream) -> None:
    sender = FakeSender(SendResult("permanent_failure", error_code="http_403"))
    await stream.publish(notice())
    await consumer(stream, sender).read_new()
    [result] = await results(stream)
    assert (result["status"], result["error_code"]) == ("dead_lettered", "http_403")
    assert await pending(stream) == 0


@needs_redis
async def test_slow_send_is_unknown(stream: NotificationStream, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(delivery_module, "MAX_SEND_SECONDS", 0.05)
    sender = FakeSender()
    sender.delay = 1.0
    await stream.publish(notice())
    await consumer(stream, sender).read_new()
    [result] = await results(stream)
    assert (result["status"], result["outcome"], result["error_code"]) == (
        "retrying",
        "unknown",
        "send_timeout",
    )


@needs_redis
async def test_entry_claimed_by_another_consumer_is_not_sent(stream: NotificationStream) -> None:
    first, second = FakeSender(), FakeSender()
    await stream.publish(notice())
    reader = consumer(stream, first, name="test-a")
    reply = await stream.client.xreadgroup(
        stream.keys.group, reader.name, {stream.keys.incoming: ">"}, count=1
    )
    entry = stream_entries(reply, stream.keys.incoming)[0]
    await consumer(stream, second, name="test-b").reclaim()
    await reader.handle(entry)
    assert first.calls == [] and len(second.calls) == 1
    assert [item["consumer"] for item in await results(stream)] == ["test-b"]


@needs_redis
async def test_deleted_pending_payload_stops_the_consumer(stream: NotificationStream) -> None:
    await stream.publish(notice())
    reply = await stream.client.xreadgroup(
        stream.keys.group, "test-dead", {stream.keys.incoming: ">"}, count=1
    )
    entry_id = stream_entries(reply, stream.keys.incoming)[0][0]
    await stream.client.xdel(stream.keys.incoming, entry_id)
    sender = FakeSender()
    stop = asyncio.Event()
    await asyncio.wait_for(consumer(stream, sender).run(stop), 2)  # Returns by itself.
    assert sender.calls == []


@needs_redis
async def test_run_delivers_published_notifications_until_stopped(stream: NotificationStream) -> None:
    sender = FakeSender()
    stop = asyncio.Event()
    task = asyncio.create_task(consumer(stream, sender).run(stop))
    await stream.publish(notice(text="первое"))
    await stream.publish(notice(text="второе"))
    for _ in range(100):
        if len(sender.calls) == 2:
            break
        await asyncio.sleep(0.02)
    stop.set()
    await asyncio.wait_for(task, 2)
    assert [text for _, text in sender.calls] == ["первое", "второе"]
    assert await pending(stream) == 0


@needs_redis
async def test_failed_result_write_keeps_entry_pending_for_reclaim(
    stream: NotificationStream, monkeypatch: pytest.MonkeyPatch
) -> None:
    from redis.exceptions import ConnectionError as RedisConnectionError

    sender = FakeSender()
    await stream.publish(notice())
    worker = consumer(stream, sender)
    real_report = stream.report
    calls = 0

    async def flaky_report(**kwargs: object) -> bool:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RedisConnectionError("redis down")
        return await real_report(**kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(stream, "report", flaky_report)
    with pytest.raises(RedisConnectionError):
        await worker.read_new()
    # Sent, but without a recorded result there is no ACK: the entry stays pending.
    assert len(sender.calls) == 1
    assert await results(stream) == []
    assert await pending(stream) == 1
    assert await worker.reclaim() == 1
    [result] = await results(stream)
    assert (result["status"], result["delivery_count"]) == ("delivered", 2)
    assert len(sender.calls) == 2  # The contract's at-least-once: a possible duplicate.
    assert await pending(stream) == 0
