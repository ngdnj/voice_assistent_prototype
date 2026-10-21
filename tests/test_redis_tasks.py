import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from redis.exceptions import ConnectionError

from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.service import TaskService
from app.planner.task import create_task
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_index import index_key
from app.storage.redis_tasks import RedisTaskRepository


CREATED_AT = datetime(2026, 10, 21, 9, 30, 12, 123456, tzinfo=UTC)


class RedisTaskErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_infrastructure_errors_are_storage_errors(self) -> None:
        for method in ("eval", "get"):
            with self.subTest(method=method):
                client = AsyncMock()
                getattr(client, method).side_effect = ConnectionError("private details")
                repository = RedisTaskRepository(client)
                task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
                with self.assertRaises(TaskStorageError) as caught:
                    if method == "eval":
                        await repository.add(task)
                    else:
                        await repository.get(task_id=task.id, owner_id=task.owner_id)
                self.assertNotIn("private details", str(caught.exception))


@unittest.skipUnless(
    os.environ.get("RUN_REDIS_INTEGRATION") == "1",
    "set RUN_REDIS_INTEGRATION=1 for a dedicated test Redis",
)
class RedisTaskIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.connection = redis_connection(RedisSettings.from_env())
        self.client = await self.connection.__aenter__()
        self.addAsyncCleanup(self.connection.__aexit__, None, None, None)
        self.prefix = f"it-gorky:test:{uuid4()}"
        self.keys: list[str] = []
        self.addAsyncCleanup(self.cleanup_keys)
        self.repository = RedisTaskRepository(self.client, key_prefix=self.prefix)

    async def cleanup_keys(self) -> None:
        async for key in self.client.scan_iter(match=f"{self.prefix}:v1:operation:*"):
            await self.client.delete(key)
        self.keys.extend(
            index_key(self.prefix, kind, owner)
            for kind in ("task", "meeting", "reminder")
            for owner in ("user-42", "other")
        )
        if self.keys:
            await self.client.delete(*self.keys)

    def key_for(self, task_id: UUID) -> str:
        key = f"{self.prefix}:v1:task:{task_id}"
        self.keys.append(key)
        return key

    async def test_service_roundtrip_through_new_repository(self) -> None:
        service = TaskService(repository=self.repository, clock=lambda: CREATED_AT)
        task = await service.create_task(owner_id="user-42", title="Отчёт 📋", operation_id="test-call-1")
        key = self.key_for(task.id)
        # A new client and repository cannot rely on the first instance's memory.
        async with redis_connection(RedisSettings.from_env()) as client:
            other = TaskService(
                repository=RedisTaskRepository(client, key_prefix=self.prefix),
                clock=lambda: CREATED_AT,
            )
            loaded = await other.get_task(task_id=task.id, owner_id=task.owner_id)
        self.assertEqual(loaded, task)
        self.assertEqual(loaded.created_at.microsecond, 123456)
        self.assertIs(loaded.created_at.tzinfo, UTC)
        self.assertEqual(await self.client.ttl(key), -1)
        self.assertEqual(
            json.loads(await self.client.get(key)),
            {
                "id": str(task.id),
                "owner_id": "user-42",
                "title": "Отчёт 📋",
                "status": "active",
                "created_at": CREATED_AT.isoformat(),
            },
        )

    async def test_absent_task(self) -> None:
        self.assertIsNone(await self.repository.get(task_id=uuid4(), owner_id="user-42"))

    async def test_owner_isolation(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        self.key_for(task.id)
        await self.repository.add(task)
        self.assertIsNone(await self.repository.get(task_id=task.id, owner_id="other"))
        self.assertEqual(await self.repository.get(task_id=task.id, owner_id="user-42"), task)

    async def test_duplicate_cannot_overwrite(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        self.key_for(task.id)
        await self.repository.add(task)
        for candidate in (task, replace(task, owner_id="other", title="Замена")):
            with self.assertRaises(TaskAlreadyExistsError):
                await self.repository.add(candidate)
        self.assertEqual(await self.repository.get(task_id=task.id, owner_id=task.owner_id), task)

    async def test_concurrent_add_has_one_winner(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        self.key_for(task.id)
        other = replace(task, title="Другое название")
        results = await asyncio.gather(
            self.repository.add(task), self.repository.add(other), return_exceptions=True
        )
        self.assertEqual(sum(not isinstance(r, BaseException) for r in results), 1)
        self.assertEqual(sum(isinstance(r, TaskAlreadyExistsError) for r in results), 1)
        stored = await self.repository.get(task_id=task.id, owner_id=task.owner_id)
        self.assertIn(stored, (task, other))

    async def test_corrupted_data_is_storage_error(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        key = self.key_for(task.id)
        good = {
            "id": str(task.id),
            "owner_id": task.owner_id,
            "title": task.title,
            "status": "active",
            "created_at": CREATED_AT.isoformat(),
        }
        bad_records = ["not JSON", "[]", "{}", b"\xff"]
        for field, value in (
            ("id", "invalid"),
            ("id", str(uuid4())),
            ("owner_id", " "),
            ("title", " "),
            ("title", " untrimmed "),
            ("title", 123),
            ("status", "done"),
            ("created_at", "2026-10-21T09:30:00"),
            ("created_at", "2026-10-21T09:30:00+03:00"),
        ):
            bad_records.append(json.dumps({**good, field: value}))
        for payload in bad_records:
            with self.subTest(payload=payload):
                await self.client.set(key, payload)
                with self.assertRaises(TaskStorageError):
                    await self.repository.get(task_id=task.id, owner_id=task.owner_id)

    async def test_wrong_redis_type_is_storage_error(self) -> None:
        task_id = uuid4()
        key = self.key_for(task_id)
        await self.client.hset(key, mapping={"unexpected": "hash"})
        with self.assertRaises(TaskStorageError):
            await self.repository.get(task_id=task_id, owner_id="user-42")
