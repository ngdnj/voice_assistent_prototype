import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from app.planner.meeting import Meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError, MeetingStorageError
from app.planner.query import QueryTooBroadError, RecordQuery
from app.planner.reminder import Reminder
from app.planner.reminder_repository import ReminderAlreadyExistsError, ReminderStorageError
from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.task import Task
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_index import index_key, index_member
from app.storage.redis_meetings import RedisMeetingRepository
from app.storage.redis_reminders import RedisReminderRepository
from app.storage.redis_tasks import RedisTaskRepository


BASE = datetime(2026, 10, 22, 8, tzinfo=UTC)
KINDS = ("task", "meeting", "reminder")
STORAGE_ERRORS = (TaskStorageError, MeetingStorageError, ReminderStorageError)
CONFLICTS = (TaskAlreadyExistsError, MeetingAlreadyExistsError, ReminderAlreadyExistsError)


@unittest.skipUnless(os.environ.get("RUN_REDIS_INTEGRATION") == "1", "enable test Redis explicitly")
class RedisQueryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        context = redis_connection(RedisSettings.from_env())
        self.client = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.prefix = f"it-gorky:test:{uuid4()}"
        self.keys: set[str] = set()
        self.addAsyncCleanup(self.cleanup_keys)
        self.repositories = {
            "task": RedisTaskRepository(self.client, key_prefix=self.prefix),
            "meeting": RedisMeetingRepository(self.client, key_prefix=self.prefix),
            "reminder": RedisReminderRepository(self.client, key_prefix=self.prefix),
        }

    async def cleanup_keys(self) -> None:
        if self.keys:
            await self.client.delete(*self.keys)

    def record(self, kind: str, record_id: int, moment: datetime, *, owner="user-42", text="Отчёт"):
        common = dict(id=UUID(int=record_id), owner_id=owner)
        if kind == "task":
            return Task(**common, title=text, created_at=moment)
        if kind == "meeting":
            return Meeting(**common, title=text, created_at=BASE - timedelta(days=1),
                           starts_at=moment, ends_at=moment + timedelta(hours=1))
        return Reminder(**common, text=text, created_at=BASE - timedelta(days=1), remind_at=moment)

    def keys_for(self, kind: str, record) -> tuple[str, str]:
        key = f"{self.prefix}:v1:{kind}:{record.id}"
        index = index_key(self.prefix, kind, record.owner_id)
        self.keys.update((key, index))
        return key, index

    async def add(self, kind: str, record) -> None:
        self.keys_for(kind, record)
        await self.repositories[kind].add(record)

    async def test_empty_owner_period_order_and_pages(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                repo = self.repositories[kind]
                self.assertEqual(await repo.list(RecordQuery(owner_id="user-42")), [])
                for number, delta, owner in (
                    (4, 1, "user-42"), (2, 0, "user-42"),
                    (1, 0, "user-42"), (3, 0, "other"), (5, 2, "user-42"),
                ):
                    await self.add(kind, self.record(kind, number, BASE + timedelta(microseconds=delta), owner=owner))
                query = RecordQuery(owner_id="user-42", start_time=BASE, end_time=BASE + timedelta(microseconds=2))
                self.assertEqual([r.id.int for r in await repo.list(query)], [1, 2, 4])
                self.assertEqual([r.id.int for r in await repo.list(replace(query, limit=1, offset=1))], [2])
                self.assertEqual(await repo.list(replace(query, offset=100)), [])
                self.assertEqual(await repo.list(RecordQuery(owner_id="nobody")), [])
                self.assertEqual([r.id.int for r in await repo.list(RecordQuery(
                    owner_id="user-42", start_time=BASE + timedelta(microseconds=2)
                ))], [5])

    async def test_casefold_search_filters_before_pagination(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                for number, text in ((1, "Не подходит"), (2, "ОтЧёТ"), (3, "отчёт второй"), (4, "Straße")):
                    await self.add(kind, self.record(kind, number, BASE, text=text))
                repo = self.repositories[kind]
                query = RecordQuery(owner_id="user-42", query=" ОТЧЁТ ", offset=1, limit=1)
                self.assertEqual([r.id.int for r in await repo.list(query)], [3])
                self.assertEqual([r.id.int for r in await repo.list(replace(query, query="STRASSE", offset=0))], [4])
                self.assertEqual(await repo.list(replace(query, query="нет совпадения")), [])
                self.assertEqual(await repo.list(replace(query, end_time=BASE)), [])

    async def test_page_size_and_search_work_limit(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                for number in range(1, 1002):
                    await self.add(kind, self.record(kind, number, BASE + timedelta(microseconds=number)))
                repo = self.repositories[kind]
                self.assertEqual(len(await repo.list(RecordQuery(owner_id="user-42", limit=100))), 100)
                with self.assertRaises(QueryTooBroadError):
                    await repo.list(RecordQuery(owner_id="user-42", query="отчёт"))
                self.assertEqual(len(await repo.list(RecordQuery(
                    owner_id="user-42", query="отчёт", end_time=BASE + timedelta(microseconds=2)
                ))), 1)

    async def test_atomic_write_and_duplicate_leave_indexes_consistent(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                record = self.record(kind, 1, BASE)
                key, index = self.keys_for(kind, record)
                await self.client.set(index, "wrong type")
                with self.assertRaises(STORAGE_ERRORS):
                    await self.repositories[kind].add(record)
                self.assertIsNone(await self.client.get(key))
                self.assertEqual(await self.client.get(index), "wrong type")
                await self.client.delete(index)
                await self.repositories[kind].add(record)
                other = replace(record, owner_id="other")
                _, other_index = self.keys_for(kind, other)
                with self.assertRaises(CONFLICTS):
                    await self.repositories[kind].add(other)
                self.assertEqual(await self.client.zcard(index), 1)
                self.assertEqual(await self.client.exists(other_index), 0)
                self.assertEqual(await self.client.ttl(index), -1)
                self.assertEqual(await self.client.ttl(key), -1)
                self.assertEqual(await self.repositories[kind].list(RecordQuery(owner_id="user-42")), [record])

    async def test_index_corruption_is_not_silently_hidden(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                record = self.record(kind, 1, BASE)
                key, index = self.keys_for(kind, record)
                await self.add(kind, record)
                payload = json.loads(await self.client.get(key))
                payload["owner_id"] = "other"
                await self.client.set(key, json.dumps(payload))
                with self.assertRaises(STORAGE_ERRORS):
                    await self.repositories[kind].list(RecordQuery(owner_id="user-42"))
                await self.client.delete(key)
                with self.assertRaises(STORAGE_ERRORS):
                    await self.repositories[kind].list(RecordQuery(owner_id="user-42"))

    async def test_explicit_backfill_existing_records_is_idempotent(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                record = self.record(kind, 1, BASE)
                _, index = self.keys_for(kind, record)
                await self.add(kind, record)
                await self.client.delete(index)  # Same JSON as pre-index versions.
                repo = self.repositories[kind]
                self.assertEqual(await repo.reindex(), 1)
                self.assertEqual(await repo.reindex(), 1)
                self.assertEqual(await self.client.zcard(index), 1)
                self.assertEqual(await repo.list(RecordQuery(owner_id="user-42")), [record])
