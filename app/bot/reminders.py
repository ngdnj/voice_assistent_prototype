"""TEMPORARY reminder scheduler until role 1's dispatcher exists: due reminders -> notification stream.

Agreed with the team as a stopgap. It reads role 4's `events` table read-only and never writes it;
Redis remembers what was published (`it-gorky:v1:bot:reminder:<id>:<unix time>`), so restarts and
several bot instances do not publish one reminder twice. A moved reminder fires again at its new
time; a deleted one does not fire. Remove this module when the dispatcher publishes reminders.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid5

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.bot import messages, metrics
from app.bot.delivery import pause
from app.bot.service import log_event
from app.storage.notification_stream import NotificationStream
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.settings import PostgresSettings
from app.storage.postgres.tables import events
from app.workers.notification import Notification

INTERVAL_S = 5.0
# Reminders missed for longer (the bot was off) are not sent: a stale reminder only confuses.
LOOKBACK = timedelta(hours=1)
PAGE_SIZE = 100
RECONNECT_PAUSE_S = 10.0
# Stable namespace for notification ids derived from reminder id and time.
NAMESPACE = UUID("6f1d5a52-3b0e-4f43-9d6a-6b3c1d0a7e21")


@dataclass(frozen=True, kw_only=True)
class DueReminder:
    reminder_id: UUID
    owner_id: str
    name: str
    description: str
    remind_at: datetime


# (since, until, after, limit) -> reminders with since < remind_at <= until, after the (time, id) cursor.
FetchDue = Callable[[datetime, datetime, tuple[datetime, UUID] | None, int], Awaitable[list[DueReminder]]]


def postgres_due_reminders(sessions: async_sessionmaker[AsyncSession]) -> FetchDue:
    async def fetch(
        since: datetime, until: datetime, after: tuple[datetime, UUID] | None, limit: int
    ) -> list[DueReminder]:
        query = (
            select(events.c.id, events.c.owner_id, events.c.name, events.c.description, events.c.scheduled_at)
            .where(
                events.c.event_type == "reminder",
                events.c.deleted_at.is_(None),
                events.c.scheduled_at > since,
                events.c.scheduled_at <= until,
            )
            .order_by(events.c.scheduled_at, events.c.id)
            .limit(limit)
        )
        if after is not None:
            query = query.where(tuple_(events.c.scheduled_at, events.c.id) > tuple_(*after))
        async with sessions() as session:
            rows = (await session.execute(query)).all()
        return [
            DueReminder(
                reminder_id=row.id,
                owner_id=row.owner_id,
                name=row.name,
                description=row.description,
                remind_at=row.scheduled_at,
            )
            for row in rows
        ]

    return fetch


def reminder_text(reminder: DueReminder) -> str:
    text = messages.REMINDER.format(name=reminder.name)
    if reminder.description.strip():
        text = f"{text}\n{reminder.description}"
    return text


class ReminderScheduler:
    def __init__(
        self,
        fetch_due: FetchDue,
        redis: Redis,
        stream: NotificationStream,
        *,
        prefix: str = "it-gorky",
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        lookback: timedelta = LOOKBACK,
        page_size: int = PAGE_SIZE,
    ) -> None:
        self._fetch = fetch_due
        self._redis = redis
        self._stream = stream
        self._prefix = prefix
        self._clock = clock
        self._lookback = lookback
        self._page_size = page_size
        # A key must outlive the lookback window, otherwise an old reminder could be selected again.
        self._ttl_s = int((lookback + timedelta(days=1)).total_seconds())

    def key(self, reminder: DueReminder) -> str:
        moment = int(reminder.remind_at.timestamp())
        return f"{self._prefix}:v1:bot:reminder:{reminder.reminder_id.hex}:{moment}"

    def notification(self, reminder: DueReminder, now: datetime) -> Notification:
        moment = int(reminder.remind_at.timestamp())
        return Notification(
            notification_id=uuid5(NAMESPACE, f"{reminder.reminder_id}:{moment}"),
            owner_id=reminder.owner_id,
            text=reminder_text(reminder),
            created_at=now,
        )

    async def tick(self) -> int:
        """Publish every due, not yet published reminder; return how many were published."""
        now = self._clock()
        since = now - self._lookback
        after: tuple[datetime, UUID] | None = None
        published = 0
        while True:
            page = await self._fetch(since, now, after, self._page_size)
            for reminder in page:
                if await self._publish_once(reminder, now):
                    published += 1
            if len(page) < self._page_size:
                return published
            after = (page[-1].remind_at, page[-1].reminder_id)

    async def _publish_once(self, reminder: DueReminder, now: datetime) -> bool:
        key = self.key(reminder)
        try:
            notification = self.notification(reminder, now)
        except ValueError:
            # Does not fit the contract (e.g. blank owner): skip it, logging once per reminder.
            if await self._redis.set(key, "invalid", nx=True, ex=self._ttl_s):
                log_event("reminder_invalid_record", reminder_id=reminder.reminder_id)
            return False
        if not await self._redis.set(key, "1", nx=True, ex=self._ttl_s):
            return False
        try:
            await self._stream.publish(notification)
        except BaseException:
            # Not published: let the next tick try again.
            try:
                await self._redis.delete(key)
            except RedisError:
                log_event("reminder_unmark_failed")
            raise
        metrics.REMINDERS_PUBLISHED.inc()
        log_event("reminder_published", reminder_id=reminder.reminder_id)
        return True

    async def run(self, stop: asyncio.Event, interval_s: float = INTERVAL_S) -> None:
        while not stop.is_set():
            try:
                await self.tick()
            except (SQLAlchemyError, RedisError, OSError, TimeoutError):
                log_event("reminder_tick_failed")
            await pause(stop, interval_s)


async def run_reminder_scheduler(
    redis: Redis, stream: NotificationStream, stop: asyncio.Event, *, prefix: str
) -> None:
    """Own a PostgreSQL pool for the scheduler; reconnect after the database was unavailable."""
    while not stop.is_set():
        try:
            async with postgres_engine(PostgresSettings.from_env()) as engine:
                log_event("reminders_started")
                scheduler = ReminderScheduler(
                    postgres_due_reminders(session_factory(engine)), redis, stream, prefix=prefix
                )
                await scheduler.run(stop)
                return
        except (SQLAlchemyError, OSError, TimeoutError, ValueError):
            log_event("reminders_postgres_unavailable")
            await pause(stop, RECONNECT_PAUSE_S)
