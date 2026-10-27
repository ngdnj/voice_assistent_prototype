import asyncio
import os
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from sqlalchemy import delete, insert

from app.bot.reminders import DueReminder, ReminderScheduler, postgres_due_reminders, reminder_text
from app.storage.notification_stream import NotificationStream, NotificationStreams
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.tables import events
from app.storage.redis import RedisSettings, redis_connection
from app.workers.notification import Notification

NOW = datetime(2026, 10, 27, 9, 0, tzinfo=UTC)
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_REDIS_INTEGRATION") != "1",
    reason="set RUN_REDIS_INTEGRATION=1 for a dedicated test Redis",
)


def reminder(minutes_ago: int = 1, *, owner_id: str = "42", name: str = "позвонить маме") -> DueReminder:
    return DueReminder(
        reminder_id=uuid4(),
        owner_id=owner_id,
        name=name,
        description="",
        remind_at=NOW - timedelta(minutes=minutes_ago),
    )


class FakeDue:
    """Mimics the SQL filter: since < remind_at <= until, (time, id) after the cursor, ordered."""

    def __init__(self, *items: DueReminder) -> None:
        self.items = list(items)
        self.calls = 0

    async def __call__(
        self, since: datetime, until: datetime, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[DueReminder]:
        self.calls += 1
        due = sorted(
            (item for item in self.items if since < item.remind_at <= until),
            key=lambda item: (item.remind_at, item.reminder_id),
        )
        if after is not None:
            due = [item for item in due if (item.remind_at, item.reminder_id) > after]
        return due[:limit]


class FlakyStream(NotificationStream):
    def __init__(self, inner: NotificationStream, failures: int) -> None:
        super().__init__(inner.client, keys=inner.keys)
        self.failures = failures

    async def publish(self, notification: Notification) -> str:
        if self.failures:
            self.failures -= 1
            raise ConnectionError("redis down")
        return await super().publish(notification)


@pytest.fixture
async def parts() -> AsyncIterator[tuple[NotificationStream, str]]:
    prefix = f"it-gorky:test:{uuid4()}"
    async with redis_connection(RedisSettings.from_env()) as client:
        stream = NotificationStream(client, keys=NotificationStreams(prefix=prefix))
        try:
            yield stream, prefix
        finally:
            # Only our own keys; never FLUSHDB.
            keys = [key async for key in client.scan_iter(match=f"{prefix}:*")]
            if keys:
                await client.delete(*keys)


def scheduler(stream: NotificationStream, prefix: str, due: FakeDue, **kwargs: object) -> ReminderScheduler:
    return ReminderScheduler(due, stream.client, stream, prefix=prefix, clock=lambda: NOW, **kwargs)  # type: ignore[arg-type]


async def published(stream: NotificationStream) -> list[dict[str, str]]:
    return [fields for _, fields in await stream.client.xrange(stream.keys.incoming)]


async def test_due_reminder_is_published_once_across_ticks_and_instances(
    parts: tuple[NotificationStream, str],
) -> None:
    stream, prefix = parts
    item = reminder()
    later = reminder(minutes_ago=-5)  # Not yet due.
    due = FakeDue(item, later)
    assert await scheduler(stream, prefix, due).tick() == 1
    assert await scheduler(stream, prefix, due).tick() == 0  # A restarted or second instance.
    [fields] = await published(stream)
    notification = Notification.from_fields(fields)
    assert notification.owner_id == "42"
    assert notification.text == "🔔 Напоминание: позвонить маме"
    key = f"{prefix}:v1:bot:reminder:{item.reminder_id.hex}:{int(item.remind_at.timestamp())}"
    assert 0 < await stream.client.ttl(key) <= int((timedelta(hours=1) + timedelta(days=1)).total_seconds())


async def test_notification_id_is_stable_for_one_reminder_time(parts: tuple[NotificationStream, str]) -> None:
    stream, prefix = parts
    item = reminder()
    worker = scheduler(stream, prefix, FakeDue())
    assert worker.notification(item, NOW).notification_id == worker.notification(item, NOW).notification_id


async def test_moved_reminder_fires_again_at_new_time(parts: tuple[NotificationStream, str]) -> None:
    stream, prefix = parts
    item = reminder(minutes_ago=10)
    due = FakeDue(item)
    assert await scheduler(stream, prefix, due).tick() == 1
    due.items = [replace(item, remind_at=NOW - timedelta(minutes=1))]
    assert await scheduler(stream, prefix, due).tick() == 1
    assert len(await published(stream)) == 2


async def test_old_reminders_outside_lookback_are_skipped(parts: tuple[NotificationStream, str]) -> None:
    stream, prefix = parts
    due = FakeDue(reminder(minutes_ago=61))
    assert await scheduler(stream, prefix, due).tick() == 0


async def test_pages_through_more_than_one_batch(parts: tuple[NotificationStream, str]) -> None:
    stream, prefix = parts
    due = FakeDue(*(reminder(minutes_ago=index + 1) for index in range(5)))
    assert await scheduler(stream, prefix, due, page_size=2).tick() == 5
    assert due.calls == 3
    assert await scheduler(stream, prefix, due, page_size=2).tick() == 0


async def test_failed_publish_is_retried_on_next_tick(parts: tuple[NotificationStream, str]) -> None:
    stream, prefix = parts
    due = FakeDue(reminder())
    flaky = FlakyStream(stream, failures=1)
    with pytest.raises(ConnectionError):
        await scheduler(flaky, prefix, due).tick()
    assert await published(stream) == []
    assert await scheduler(flaky, prefix, due).tick() == 1


async def test_invalid_record_is_skipped_and_others_published(parts: tuple[NotificationStream, str]) -> None:
    stream, prefix = parts
    due = FakeDue(reminder(minutes_ago=2, owner_id=" "), reminder(minutes_ago=1))
    assert await scheduler(stream, prefix, due).tick() == 1
    assert await scheduler(stream, prefix, due).tick() == 0


def test_reminder_text_adds_description() -> None:
    item = replace(reminder(), description="взять документы")
    assert reminder_text(item) == "🔔 Напоминание: позвонить маме\nвзять документы"


@pytest.mark.skipif(os.environ.get("RUN_PG_INTEGRATION") != "1", reason="set RUN_PG_INTEGRATION=1")
async def test_postgres_query_selects_due_live_reminders_in_order() -> None:
    from tests.storage_pg import integration_settings, migrate_test_database

    await asyncio.to_thread(migrate_test_database)  # Alembic runs its own event loop.
    owner = f"test-{uuid4()}"
    created = NOW - timedelta(days=1)

    def row(event_type: str, minutes_ago: int, **extra: object) -> dict[str, object]:
        values: dict[str, object] = {
            "id": uuid4(),
            "owner_id": owner,
            "event_type": event_type,
            "name": f"{event_type} {minutes_ago}",
            "scheduled_at": NOW - timedelta(minutes=minutes_ago),
            "created_at": created,
            "updated_at": created,
            # Every row lists the same columns: one multi-row INSERT.
            "status": "todo" if event_type == "task" else None,
            "deleted_at": None,
        }
        values.update(extra)
        return values

    rows = [
        row("reminder", 5),
        row("reminder", 1),
        row("reminder", 3, deleted_at=NOW),
        row("reminder", -5),  # Not yet due.
        row("reminder", 90),  # Outside the window.
        row("meeting", 2),
        row("task", 2),
    ]
    async with postgres_engine(integration_settings()) as engine:
        sessions = session_factory(engine)
        async with sessions() as session, session.begin():
            await session.execute(insert(events), rows)
        try:
            fetch = postgres_due_reminders(sessions)
            found = [
                item
                for item in await fetch(NOW - timedelta(hours=1), NOW, None, 100)
                if item.owner_id == owner
            ]
            assert [item.name for item in found] == ["reminder 5", "reminder 1"]
            first = found[0]
            after = await fetch(NOW - timedelta(hours=1), NOW, (first.remind_at, first.reminder_id), 100)
            assert [item.name for item in after if item.owner_id == owner] == ["reminder 1"]
            assert all(item.remind_at.tzinfo is not None for item in found)
        finally:
            async with sessions() as session, session.begin():
                await session.execute(delete(events).where(events.c.owner_id == owner))


async def test_reminder_reaches_the_user_through_the_stream(parts: tuple[NotificationStream, str]) -> None:
    from app.bot.client import SendResult
    from app.bot.delivery import DeliveryConsumer

    class Sender:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def send_to_user(self, *, user_id: str, text: str) -> SendResult:
            self.calls.append((user_id, text))
            return SendResult("delivered", external_message_id="mid.9")

    stream, prefix = parts
    await stream.ensure_group()
    await scheduler(stream, prefix, FakeDue(reminder())).tick()
    sender = Sender()
    await DeliveryConsumer(stream, sender, name="test-e2e", clock=lambda: NOW, block_ms=10).read_new()
    assert sender.calls == [("42", "🔔 Напоминание: позвонить маме")]
