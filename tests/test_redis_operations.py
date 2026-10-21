import asyncio
import os
import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import patch
from uuid import uuid4

from redis.exceptions import ConnectionError

from app.planner.meeting_repository import MeetingStorageError
from app.planner.meeting_service import MeetingService
from app.planner.operation import OperationConflictError
from app.planner.query import RecordQuery
from app.planner.reminder_repository import ReminderStorageError
from app.planner.reminder_service import ReminderService
from app.planner.repository import TaskStorageError
from app.planner.service import TaskService
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_index import index_key
from app.storage.redis_meetings import RedisMeetingRepository
from app.storage.redis_operation import CREATE_ONCE, operation_key
from app.storage.redis_reminders import RedisReminderRepository
from app.storage.redis_tasks import RedisTaskRepository

BASE = datetime(2026, 10, 22, 8, tzinfo=UTC)
KINDS = ("task", "meeting", "reminder")
STORAGE_ERRORS = (TaskStorageError, MeetingStorageError, ReminderStorageError)


@unittest.skipUnless(os.environ.get("RUN_REDIS_INTEGRATION") == "1", "enable test Redis explicitly")
class RedisOperationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        context = redis_connection(RedisSettings.from_env())
        self.client = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.prefix = f"it-gorky:test:{uuid4()}"
        self.keys: set[str] = set()
        self.record_keys: set[str] = set()
        self.addAsyncCleanup(self.cleanup_keys)
        self.original_eval = self.client.eval

        async def tracked_eval(script, numkeys, *args):
            if script == CREATE_ONCE:
                self.keys.update(args[:numkeys])
                self.record_keys.add(args[0])
            return await self.original_eval(script, numkeys, *args)

        tracker = patch.object(self.client, "eval", side_effect=tracked_eval)
        tracker.start()
        self.addCleanup(tracker.stop)

    async def cleanup_keys(self) -> None:
        if self.keys:
            await self.client.delete(*self.keys)

    def repository(self, kind, client=None):
        classes = {
            "task": RedisTaskRepository,
            "meeting": RedisMeetingRepository,
            "reminder": RedisReminderRepository,
        }
        return classes[kind](self.client if client is None else client, key_prefix=self.prefix)

    async def create(
        self,
        kind,
        *,
        owner="user-42",
        operation_id="one",
        text="Отчёт",
        clock=lambda: BASE,
        client=None,
        delta=0,
        offset=0,
    ):
        repository = self.repository(kind, client)
        common = dict(owner_id=owner, operation_id=operation_id)
        due = (BASE + timedelta(hours=1, seconds=delta)).astimezone(timezone(timedelta(hours=offset)))
        if kind == "task":
            return await TaskService(repository=repository, clock=clock).create_task(**common, title=text)
        if kind == "meeting":
            return await MeetingService(repository=repository, clock=clock).create_meeting(
                **common, title=text, starts_at=due, ends_at=due + timedelta(minutes=45)
            )
        return await ReminderService(repository=repository, clock=clock).create_reminder(
            **common, text=text, remind_at=due
        )

    async def test_replay_from_new_client_after_time_advance(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                original = await self.create(kind, text=" Отчёт ")
                async with redis_connection(RedisSettings.from_env()) as client:
                    replay = await self.create(
                        kind, client=client, offset=3, clock=lambda: BASE + timedelta(days=1)
                    )
                self.assertEqual(replay, original)
                op_key = operation_key(self.prefix, kind, "user-42", "one")
                self.assertEqual(await self.client.ttl(op_key), -1)
                self.assertEqual(await self.client.zcard(index_key(self.prefix, kind, "user-42")), 1)

    async def test_concurrent_identical_requests(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                results = await asyncio.gather(*(self.create(kind) for _ in range(10)))
                self.assertTrue(all(record == results[0] for record in results))
                repo = self.repository(kind)
                self.assertEqual(await repo.list(RecordQuery(owner_id="user-42")), [results[0]])
                kind_keys = [key for key in self.record_keys if f":v1:{kind}:" in key]
                self.assertEqual(sum([await self.client.exists(key) for key in kind_keys]), 1)

    async def test_conflict_keeps_first_result(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                original = await self.create(kind)
                with self.assertRaises(OperationConflictError):
                    await self.create(kind, text="Другие данные")
                if kind != "task":
                    with self.assertRaises(OperationConflictError):
                        await self.create(kind, delta=1)
                self.assertEqual(await self.create(kind), original)
                self.assertEqual(
                    await self.repository(kind).list(RecordQuery(owner_id="user-42")), [original]
                )

    async def test_concurrent_different_data_conflicts_inside_commit(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                # Both callers miss the preliminary lookup; only Lua can arbitrate.
                with patch.object(type(self.repository(kind)), "get_created", return_value=None):
                    results = await asyncio.gather(
                        self.create(kind, text="Первый"),
                        self.create(kind, text="Второй"),
                        return_exceptions=True,
                    )
                self.assertEqual(sum(isinstance(r, OperationConflictError) for r in results), 1)
                stored = await self.repository(kind).list(RecordQuery(owner_id="user-42"))
                self.assertEqual(len(stored), 1)
                self.assertIn(stored[0], results)

    async def test_owner_and_operation_type_isolation(self) -> None:
        results = []
        for kind in KINDS:
            for owner in ("user-42", "other"):
                results.append(await self.create(kind, owner=owner))
                self.assertEqual(len(await self.repository(kind).list(RecordQuery(owner_id=owner))), 1)
        self.assertEqual(len({record.id for record in results}), 6)

    async def test_response_lost_after_commit_does_not_create_again(self) -> None:
        tracked = self.client.eval

        async def lose_reply(*args, **kwargs):
            await tracked(*args, **kwargs)
            raise ConnectionError("simulated lost response")

        for kind in KINDS:
            with self.subTest(kind=kind):
                with (
                    patch.object(self.client, "eval", side_effect=lose_reply),
                    self.assertRaises(STORAGE_ERRORS),
                ):
                    await self.create(kind)
                recovered = await self.create(kind, clock=lambda: BASE + timedelta(days=1))
                self.assertEqual(recovered.created_at, BASE)
                self.assertEqual(
                    await self.repository(kind).list(RecordQuery(owner_id="user-42")), [recovered]
                )

    async def test_invalid_index_leaves_no_record_or_operation(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                index = index_key(self.prefix, kind, "user-42")
                op_key = operation_key(self.prefix, kind, "user-42", "one")
                self.keys.add(index)
                await self.client.set(index, "wrong type")
                with self.assertRaises(STORAGE_ERRORS):
                    await self.create(kind)
                self.assertEqual(await self.client.exists(op_key), 0)
                for key in self.record_keys:
                    self.assertEqual(await self.client.exists(key), 0)
                self.assertEqual(await self.client.get(index), "wrong type")
                await self.client.delete(index)
                recovered = await self.create(kind)
                self.assertEqual(await self.create(kind), recovered)
                # Remove this kind before the next subtest checks its own failed candidates.
                await self.client.delete(*self.keys)

    async def test_corrupt_operation_prevents_new_creation(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                key = operation_key(self.prefix, kind, "user-42", "one")
                self.keys.add(key)
                for value in ("broken",):
                    await self.client.set(key, value)
                    with self.assertRaises(STORAGE_ERRORS):
                        await self.create(kind)
                await self.client.delete(key)
                await self.client.hset(key, mapping={"unexpected": "hash"})
                with self.assertRaises(STORAGE_ERRORS):
                    await self.create(kind)
                self.assertEqual(await self.repository(kind).list(RecordQuery(owner_id="user-42")), [])

    async def test_missing_record_does_not_discard_operation_guard(self) -> None:
        for kind in KINDS:
            with self.subTest(kind=kind):
                original = await self.create(kind)
                key = f"{self.prefix}:v1:{kind}:{original.id}"
                await self.client.delete(key)
                with self.assertRaises(STORAGE_ERRORS):
                    await self.create(kind)
                self.assertEqual(await self.client.exists(key), 0)
                self.assertEqual(
                    await self.client.exists(operation_key(self.prefix, kind, "user-42", "one")), 1
                )
