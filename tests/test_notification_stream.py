import json
import os
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from redis.exceptions import ConnectionError, ResponseError

from app.storage.notification_stream import NotificationStream, NotificationStreams, StaleDeliveryError
from app.storage.redis import RedisSettings, redis_connection
from app.workers.notification import Notification
from tests.notification_consumer import FakeOutcome, TestNotificationConsumer

CREATED = datetime(2026, 10, 22, 8, tzinfo=UTC)


class NotificationTests(unittest.TestCase):
    def test_roundtrip_and_explicit_time(self) -> None:
        notice = Notification(
            notification_id=UUID(int=1),
            owner_id="user-42",
            text="Напоминание 📋",
            created_at=CREATED.astimezone(timezone(timedelta(hours=3))),
        )
        self.assertEqual(Notification.from_fields(notice.to_fields()), notice)
        self.assertIs(notice.created_at.tzinfo, UTC)
        self.assertEqual(notice.created_at, CREATED)

    def test_invalid_envelopes(self) -> None:
        good = Notification(
            notification_id=UUID(int=1), owner_id="user-42", text="Текст", created_at=CREATED
        ).to_fields()
        for fields in (
            {},
            {**good, "version": "2"},
            {**good, "notification_id": "invalid"},
            {**good, "owner_id": " "},
            {**good, "text": " "},
            {**good, "text": "x" * 4001},
            {**good, "created_at": "2026-10-22T08:00:00"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                Notification.from_fields(fields)


@unittest.skipUnless(os.environ.get("RUN_REDIS_INTEGRATION") == "1", "enable test Redis explicitly")
class NotificationStreamTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        context = redis_connection(RedisSettings.from_env())
        self.client = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.keys = NotificationStreams(prefix=f"it-gorky:test:{uuid4()}")
        self.stream = NotificationStream(self.client, keys=self.keys)

        async def clean_streams() -> None:
            await self.client.delete(self.keys.incoming, self.keys.results, self.keys.dead)

        self.addAsyncCleanup(clean_streams)
        self.notice = Notification(
            notification_id=uuid4(), owner_id="user-42", text="Позвонить", created_at=CREATED
        )
        self.first = TestNotificationConsumer(self.stream, "process-a", clock=lambda: CREATED)
        self.second = TestNotificationConsumer(self.stream, "process-b", clock=lambda: CREATED)

    async def publish_and_read(self):
        entry_id = await self.stream.publish(self.notice)
        await self.stream.ensure_group()
        entries = await self.first.read_new()
        self.assertEqual(entries[0][0], entry_id)
        return entries[0]

    async def pending(self) -> int:
        return (await self.client.xpending(self.keys.incoming, self.keys.group))["pending"]

    async def results(self):
        return [json.loads(fields["data"]) for _, fields in await self.client.xrange(self.keys.results)]

    async def test_publish_read_record_success_and_ack(self) -> None:
        entry = await self.publish_and_read()  # Publication before group creation must not be skipped.
        await self.stream.ensure_group()
        self.assertEqual(await self.pending(), 1)
        self.assertEqual(Notification.from_fields(entry[1]), self.notice)
        sender = AsyncMock(return_value=FakeOutcome("delivered", external_message_id="fake-1"))
        await self.first.handle(entry, sender)
        sender.assert_awaited_once_with(self.notice)
        self.assertEqual(await self.pending(), 0)
        result = (await self.results())[0]
        self.assertEqual(result["status"], "delivered")
        self.assertEqual(result["notification_id"], str(self.notice.notification_id))
        self.assertEqual(result["owner_id"], "user-42")
        self.assertEqual(result["external_message_id"], "fake-1")
        self.assertEqual(await self.client.xlen(self.keys.dead), 0)
        # XACK clears PEL, not the stream entry.
        self.assertEqual(await self.client.xlen(self.keys.incoming), 1)
        self.assertEqual(await self.first.read_new(), [])
        self.assertFalse(
            await self.stream.report(
                entry_id=entry[0],
                consumer="process-a",
                delivery_count=1,
                status="delivered",
                fields=entry[1],
                recorded_at=CREATED,
                outcome="delivered",
            )
        )
        self.assertEqual(len(await self.results()), 1)

    async def test_recovery_and_stale_consumer_cannot_ack(self) -> None:
        entry = await self.publish_and_read()
        _, fresh = await self.second.recover()  # No elapsed 60-second lease yet.
        self.assertEqual(fresh, [])
        cursor, recovered = await self.second.recover(min_idle_ms=0)
        self.assertEqual(cursor, "0-0")
        self.assertEqual(recovered, [entry])
        sender = AsyncMock(return_value=FakeOutcome("delivered"))
        with self.assertRaises(StaleDeliveryError):
            await self.first.handle(entry, sender)
        sender.assert_not_awaited()
        with self.assertRaises(StaleDeliveryError):
            await self.stream.report(
                entry_id=entry[0],
                consumer="process-a",
                delivery_count=1,
                status="delivered",
                fields=entry[1],
                recorded_at=CREATED,
                outcome="delivered",
            )
        await self.second.handle(recovered[0], sender)
        self.assertEqual(await self.pending(), 0)
        self.assertEqual((await self.results())[0]["delivery_count"], 2)

    async def test_retry_budget_then_dead_letter(self) -> None:
        entry = await self.publish_and_read()
        sender = AsyncMock(return_value=FakeOutcome("retryable_failure", "network_temporary"))
        for count in range(1, 4):
            await self.first.handle(entry, sender)
            self.assertEqual(await self.pending(), 1 if count < 3 else 0)
            if count < 3:
                _, recovered = await self.first.recover(min_idle_ms=0)
                entry = recovered[0]
        self.assertEqual(sender.await_count, 3)
        self.assertEqual(
            [r["status"] for r in await self.results()], ["retrying", "retrying", "dead_lettered"]
        )
        dead = json.loads((await self.client.xrange(self.keys.dead))[0][1]["data"])
        self.assertEqual(dead["envelope"], self.notice.to_fields())
        self.assertEqual(dead["delivery_count"], 3)
        self.assertEqual(dead["error_code"], "network_temporary")

    async def test_crashes_before_handling_exhaust_budget_without_sending(self) -> None:
        await self.publish_and_read()
        recovered = []
        for _ in range(3):
            _, recovered = await self.second.recover(min_idle_ms=0)
        sender = AsyncMock(return_value=FakeOutcome("delivered"))
        await self.second.handle(recovered[0], sender)
        sender.assert_not_awaited()
        self.assertEqual(await self.pending(), 0)
        self.assertEqual((await self.results())[0]["error_code"], "attempts_exhausted")

    async def test_permanent_failure(self) -> None:
        entry = await self.publish_and_read()
        sender = AsyncMock(return_value=FakeOutcome("permanent_failure", "recipient_unavailable"))
        await self.first.handle(entry, sender)
        self.assertEqual(await self.pending(), 0)
        self.assertEqual(await self.client.xlen(self.keys.dead), 1)

    async def test_invalid_version_is_not_sent(self) -> None:
        fields = {**self.notice.to_fields(), "version": "2"}
        await self.client.xadd(self.keys.incoming, fields)
        await self.stream.ensure_group()
        entry = (await self.first.read_new())[0]
        sender = AsyncMock()
        await self.first.handle(entry, sender)
        sender.assert_not_awaited()
        self.assertEqual(await self.pending(), 0)
        self.assertEqual((await self.results())[0]["error_code"], "invalid_envelope")

    async def test_crash_after_external_send_can_duplicate_delivery(self) -> None:
        entry = await self.publish_and_read()
        sender = AsyncMock(return_value=FakeOutcome("delivered"))
        with (
            patch.object(self.stream, "report", side_effect=ConnectionError("simulated crash")),
            self.assertRaises(ConnectionError),
        ):
            await self.first.handle(entry, sender)
        self.assertEqual(await self.pending(), 1)
        _, recovered = await self.second.recover(min_idle_ms=0)
        await self.second.handle(recovered[0], sender)
        self.assertEqual(sender.await_count, 2)  # Explicitly demonstrates at-least-once, not exactly-once.
        self.assertEqual(await self.pending(), 0)

    async def test_result_failure_does_not_ack(self) -> None:
        entry = await self.publish_and_read()
        await self.client.set(self.keys.results, "wrong type")
        sender = AsyncMock(return_value=FakeOutcome("delivered"))
        with self.assertRaises(ResponseError):
            await self.first.handle(entry, sender)
        self.assertEqual(await self.pending(), 1)
        self.assertEqual(await self.client.get(self.keys.results), "wrong type")
        self.assertEqual(await self.client.exists(self.keys.dead), 0)
