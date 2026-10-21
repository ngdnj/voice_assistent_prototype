"""One-shot contract demonstrator for tests only. This is not a production worker."""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal
from uuid import uuid4

from app.storage.notification_stream import (
    MAX_DELIVERIES,
    MAX_SEND_SECONDS,
    RECLAIM_IDLE_MS,
    NotificationStream,
    NotificationStreams,
    ResultStatus,
    StaleDeliveryError,
)
from app.storage.redis import RedisSettings, redis_connection
from app.workers.notification import Notification


@dataclass(frozen=True)
class FakeOutcome:
    status: Literal["delivered", "retryable_failure", "permanent_failure", "unknown"]
    error_code: str = ""
    external_message_id: str = ""


class TestNotificationConsumer:
    """No loop or MAX dependencies: callers explicitly read, claim, then handle."""

    __test__ = False

    def __init__(self, stream: NotificationStream, name: str, *, clock: Callable[[], datetime]) -> None:
        self.stream = stream
        self.name = name
        self.clock = clock

    async def read_new(self) -> list[tuple[str, dict[str, str]]]:
        batches = await self.stream.client.xreadgroup(
            self.stream.keys.group, self.name, {self.stream.keys.incoming: ">"}, count=1
        )
        return [] if not batches else batches[0][1]

    async def recover(
        self, *, start_id: str = "0-0", min_idle_ms: int = RECLAIM_IDLE_MS
    ) -> tuple[str, list[tuple[str, dict[str, str]]]]:
        reply = await self.stream.client.xautoclaim(
            self.stream.keys.incoming,
            self.stream.keys.group,
            self.name,
            min_idle_time=min_idle_ms,
            start_id=start_id,
            count=10,
        )
        if reply[2]:
            raise RuntimeError("Pending payloads were deleted; investigate stream retention")
        return reply[0], reply[1]

    async def handle(
        self, entry: tuple[str, dict[str, str]], sender: Callable[[Notification], Awaitable[FakeOutcome]]
    ) -> None:
        entry_id, fields = entry
        pending = await self.stream.client.xpending_range(
            self.stream.keys.incoming, self.stream.keys.group, entry_id, entry_id, 1
        )
        if not pending or pending[0]["consumer"] != self.name:
            raise StaleDeliveryError("Consumer does not own this pending entry")
        count = pending[0]["times_delivered"]
        try:
            notification = Notification.from_fields(fields)
        except (ValueError, TypeError):
            await self._report(entry, count, "dead_lettered", "not_attempted", "invalid_envelope")
            return
        if count > MAX_DELIVERIES:
            await self._report(entry, count, "dead_lettered", "not_attempted", "attempts_exhausted")
            return
        try:
            result = await asyncio.wait_for(sender(notification), timeout=MAX_SEND_SECONDS)
        except TimeoutError:
            result = FakeOutcome("unknown", "sender_timeout_unknown")
        if result.status == "delivered":
            await self._report(entry, count, "delivered", result.status, "", result.external_message_id)
        else:
            status: ResultStatus = (
                "dead_lettered"
                if count >= MAX_DELIVERIES or result.status == "permanent_failure"
                else "retrying"
            )
            await self._report(entry, count, status, result.status, result.error_code)

    async def _report(
        self,
        entry: tuple[str, dict[str, str]],
        count: int,
        status: ResultStatus,
        outcome: str,
        error_code: str,
        external_message_id: str = "",
    ) -> None:
        await self.stream.report(
            entry_id=entry[0],
            fields=entry[1],
            consumer=self.name,
            delivery_count=count,
            status=status,
            outcome=outcome,
            error_code=error_code,
            external_message_id=external_message_id,
            recorded_at=self.clock(),
        )


async def demo() -> None:
    keys = NotificationStreams(prefix=f"it-gorky:demo:{uuid4()}")
    async with redis_connection(RedisSettings.from_env()) as client:
        try:
            stream = NotificationStream(client, keys=keys)
            notification = Notification(
                notification_id=uuid4(),
                owner_id="demo-user",
                text="Тест контракта, без MAX",
                created_at=datetime(2026, 10, 22, 8, tzinfo=UTC),
            )
            entry_id = await stream.publish(notification)
            await stream.ensure_group()
            consumer = TestNotificationConsumer(stream, "demo-only", clock=lambda: notification.created_at)

            async def fake_sender(value: Notification) -> FakeOutcome:
                return FakeOutcome("delivered", external_message_id="fake-message")

            entries = await consumer.read_new()
            await consumer.handle(entries[0], fake_sender)
            pending = await client.xpending(keys.incoming, keys.group)
            print(f"Demo only: {entry_id}; pending={pending['pending']}")
        finally:
            await client.delete(keys.incoming, keys.results, keys.dead)


if __name__ == "__main__":
    asyncio.run(demo())
