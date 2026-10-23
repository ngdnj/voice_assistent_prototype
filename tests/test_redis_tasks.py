import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

from redis.exceptions import ConnectionError

from app.planner.query import RecordQuery
from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.service import TaskService
from app.planner.task import create_task
from app.storage.redis import RedisSettings, redis_connection
from app.planner.operation import CreationOperation
from app.storage.redis_index import index_key, index_member
from app.storage.redis_operation import operation_key
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
                "description": "",
                "status": "todo",
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
            "status": "todo",
            "created_at": CREATED_AT.isoformat(),
        }
        bad_records = ["not JSON", "[]", "{}", b"\xff"]
        for field, value in (
            ("id", "invalid"),
            ("id", str(uuid4())),
            ("owner_id", " "),
            ("title", " "),
            ("title", " untrimmed "),
            ("description", " untrimmed "),
            ("description", 123),
            ("description", "x" * 2001),
            ("title", 123),
            ("status", "archived"),
            ("status", "TODO"),
            ("created_at", "2026-10-21T09:30:00"),
            ("created_at", "2026-10-21T09:30:00+03:00"),
        ):
            bad_records.append(json.dumps({**good, field: value}))
        for payload in bad_records:
            with self.subTest(payload=payload):
                await self.client.set(key, payload)
                with self.assertRaises(TaskStorageError):
                    await self.repository.get(task_id=task.id, owner_id=task.owner_id)

    async def test_stored_statuses_and_legacy_active(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        key = self.key_for(task.id)
        good = {
            "id": str(task.id),
            "owner_id": task.owner_id,
            "title": task.title,
            "created_at": CREATED_AT.isoformat(),
        }
        for stored, expected in (
            ("todo", "todo"), ("in_progress", "in_progress"), ("done", "done"), ("active", "todo")
        ):
            with self.subTest(stored=stored):
                await self.client.set(key, json.dumps({**good, "status": stored}))
                loaded = await self.repository.get(task_id=task.id, owner_id=task.owner_id)
                self.assertIsNotNone(loaded)
                assert loaded is not None
                self.assertEqual(loaded.status, expected)

    async def test_description_roundtrip_and_legacy_record(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT, description=" Квартальный ")
        key = self.key_for(task.id)
        await self.repository.add(task)
        self.assertEqual(json.loads(await self.client.get(key))["description"], "Квартальный")
        self.assertEqual(await self.repository.get(task_id=task.id, owner_id="user-42"), task)
        legacy = {
            "id": str(task.id), "owner_id": "user-42", "title": "Отчёт",
            "status": "todo", "created_at": CREATED_AT.isoformat(),
        }
        await self.client.set(key, json.dumps(legacy))
        loaded = await self.repository.get(task_id=task.id, owner_id="user-42")
        assert loaded is not None
        self.assertEqual(loaded.description, "")

    async def test_deadline_roundtrip(self) -> None:
        for due in (
            {"due_date": date(2026, 10, 24)},
            {"due_at": datetime(2026, 10, 24, 15, 0, 0, 123456, tzinfo=UTC)},
            {},
        ):
            with self.subTest(due=due):
                task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT, **due)
                key = self.key_for(task.id)
                await self.repository.add(task)
                stored = json.loads(await self.client.get(key))
                self.assertEqual(
                    {name: stored[name] for name in ("due_date", "due_at") if name in stored},
                    {name: value.isoformat() for name, value in due.items()},
                )
                self.assertEqual(await self.repository.get(task_id=task.id, owner_id="user-42"), task)

    async def test_corrupted_deadline_is_storage_error(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        key = self.key_for(task.id)
        good = {
            "id": str(task.id),
            "owner_id": task.owner_id,
            "title": task.title,
            "status": "todo",
            "created_at": CREATED_AT.isoformat(),
        }
        for extra in (
            {"due_date": "2026-10-24", "due_at": "2026-10-24T15:00:00+00:00"},
            {"due_date": "20261024"},
            {"due_date": "2026-10-24T00:00:00"},
            {"due_date": None},
            {"due_at": "2026-10-24T18:00:00+03:00"},
            {"due_at": "2026-10-24T18:00:00"},
            {"due_at": "2026-10-24"},
        ):
            with self.subTest(extra=extra):
                await self.client.set(key, json.dumps({**good, **extra}))
                with self.assertRaises(TaskStorageError):
                    await self.repository.get(task_id=task.id, owner_id=task.owner_id)

    async def test_status_update_persists_and_respects_owner(self) -> None:
        service = TaskService(repository=self.repository, clock=lambda: CREATED_AT)
        task = await service.create_task(
            owner_id="user-42", title="Отчёт", description="Квартальный", operation_id="status-1"
        )
        key = self.key_for(task.id)
        before = json.loads(await self.client.get(key))
        self.assertIsNone(await service.update_status(task_id=task.id, owner_id="other", status="done"))
        self.assertIsNone(await service.update_status(task_id=uuid4(), owner_id="user-42", status="done"))
        updated = await service.update_status(task_id=task.id, owner_id="user-42", status="in_progress")
        assert updated is not None
        self.assertEqual(updated.status, "in_progress")
        # Only the status field changes; the record keeps no TTL.
        self.assertEqual(json.loads(await self.client.get(key)), {**before, "status": "in_progress"})
        self.assertEqual(await self.client.ttl(key), -1)
        async with redis_connection(RedisSettings.from_env()) as client:
            loaded = await RedisTaskRepository(client, key_prefix=self.prefix).get(task_id=task.id, owner_id="user-42")
        self.assertEqual(loaded, updated)
        listed = await self.repository.list(RecordQuery(owner_id="user-42"))
        self.assertEqual(listed, [updated])
        # Replaying the creation returns the same task in its current state, not an error.
        replay = await service.create_task(
            owner_id="user-42", title="Отчёт", description="Квартальный", operation_id="status-1"
        )
        self.assertEqual(replay, updated)

    async def test_status_update_of_legacy_active_record(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        key = self.key_for(task.id)
        await self.client.set(key, json.dumps({
            "id": str(task.id), "owner_id": "user-42", "title": "Отчёт",
            "status": "active", "created_at": CREATED_AT.isoformat(),
        }))
        updated = await self.repository.update_status(task_id=task.id, owner_id="user-42", status="done")
        assert updated is not None
        self.assertEqual(json.loads(await self.client.get(key))["status"], "done")

    async def test_legacy_record_and_operation_survive_status_change(self) -> None:
        # Data written before B0: status "active", no description, fingerprint of {title} only.
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        key = self.key_for(task.id)
        legacy = json.dumps({
            "id": str(task.id), "owner_id": "user-42", "title": "Отчёт",
            "status": "active", "created_at": CREATED_AT.isoformat(),
        }, ensure_ascii=False)
        operation = CreationOperation.from_data("legacy-op", title="Отчёт")
        await self.client.set(key, legacy)
        await self.client.zadd(index_key(self.prefix, "task", "user-42"), {index_member(CREATED_AT, task.id): 0})
        await self.client.set(
            operation_key(self.prefix, "task", "user-42", "legacy-op"), f"{operation.fingerprint}\n{legacy}"
        )
        service = TaskService(repository=self.repository, clock=lambda: CREATED_AT)
        replay = await service.create_task(owner_id="user-42", title="Отчёт", operation_id="legacy-op")
        self.assertEqual((replay.id, replay.status, replay.description), (task.id, "todo", ""))
        await service.update_status(task_id=task.id, owner_id="user-42", status="done")
        replay = await service.create_task(owner_id="user-42", title="Отчёт", operation_id="legacy-op")
        self.assertEqual((replay.id, replay.status), (task.id, "done"))
        self.assertEqual(
            [t.status for t in await self.repository.list(RecordQuery(owner_id="user-42"))], ["done"]
        )

    async def test_concurrent_status_change_is_not_overwritten_silently(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        key = self.key_for(task.id)
        await self.repository.add(task)
        original_get = self.client.get
        writes = iter(range(100))

        async def changed_by_someone_else(name):
            payload = await original_get(name)
            # Another writer changes the record between our read and compare-and-set.
            data = json.loads(payload)
            data["title"] = f"Изменено {next(writes)}"
            await self.client.set(key, json.dumps(data, ensure_ascii=False))
            return payload

        with patch.object(self.client, "get", side_effect=changed_by_someone_else):
            with self.assertRaises(TaskStorageError):
                await self.repository.update_status(task_id=task.id, owner_id="user-42", status="done")
        self.assertEqual(json.loads(await self.client.get(key))["status"], "todo")

    async def test_wrong_redis_type_is_storage_error(self) -> None:
        task_id = uuid4()
        key = self.key_for(task_id)
        await self.client.hset(key, mapping={"unexpected": "hash"})
        with self.assertRaises(TaskStorageError):
            await self.repository.get(task_id=task_id, owner_id="user-42")
