import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from redis.exceptions import ConnectionError

from app.planner.meeting import create_meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError, MeetingStorageError
from app.planner.meeting_service import MeetingService
from app.planner.task import Task
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_meetings import RedisMeetingRepository
from app.storage.redis_tasks import RedisTaskRepository


CREATED = datetime(2026, 10, 22, 8, 0, 0, 123456, tzinfo=UTC)
START = CREATED + timedelta(hours=1)
END = START + timedelta(minutes=45)


class RedisMeetingErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_infrastructure_errors(self) -> None:
        for method in ("set", "get"):
            with self.subTest(method=method):
                client = AsyncMock()
                getattr(client, method).side_effect = ConnectionError("private data")
                repository = RedisMeetingRepository(client)
                with self.assertRaises(MeetingStorageError) as caught:
                    if method == "set":
                        await repository.add(create_meeting(
                            owner_id="user-42", title="Обсуждение", starts_at=START,
                            ends_at=END, created_at=CREATED,
                        ))
                    else:
                        await repository.get(meeting_id=uuid4(), owner_id="user-42")
                self.assertNotIn("private data", str(caught.exception))


@unittest.skipUnless(
    os.environ.get("RUN_REDIS_INTEGRATION") == "1",
    "set RUN_REDIS_INTEGRATION=1 for a dedicated test Redis",
)
class RedisMeetingIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.connection = redis_connection(RedisSettings.from_env())
        self.client = await self.connection.__aenter__()
        self.addAsyncCleanup(self.connection.__aexit__, None, None, None)
        self.prefix = f"it-gorky:test:{uuid4()}"
        self.keys: list[str] = []
        self.addAsyncCleanup(self.cleanup_keys)
        self.repository = RedisMeetingRepository(self.client, key_prefix=self.prefix)
        self.meeting = create_meeting(
            owner_id="user-42", title="Обсуждение 📋", starts_at=START,
            ends_at=END, created_at=CREATED,
        )

    async def cleanup_keys(self) -> None:
        if self.keys:
            await self.client.delete(*self.keys)

    def key_for(self, meeting_id: UUID) -> str:
        key = f"{self.prefix}:v1:meeting:{meeting_id}"
        self.keys.append(key)
        return key

    async def test_service_roundtrip_with_new_client_and_repository(self) -> None:
        service = MeetingService(repository=self.repository, clock=lambda: CREATED)
        meeting = await service.create_meeting(
            owner_id="user-42", title="Обсуждение 📋", starts_at=START, ends_at=END
        )
        key = self.key_for(meeting.id)
        async with redis_connection(RedisSettings.from_env()) as client:
            service = MeetingService(
                repository=RedisMeetingRepository(client, key_prefix=self.prefix),
                clock=lambda: CREATED,
            )
            loaded = await service.get_meeting(meeting_id=meeting.id, owner_id="user-42")
        self.assertEqual(loaded, meeting)
        self.assertEqual(await self.client.ttl(key), -1)
        self.assertEqual(json.loads(await self.client.get(key)), {
            "id": str(meeting.id), "owner_id": "user-42", "title": "Обсуждение 📋",
            "starts_at": START.isoformat(), "ends_at": END.isoformat(),
            "created_at": CREATED.isoformat(),
        })

    async def test_missing_and_foreign_meetings(self) -> None:
        self.assertIsNone(await self.repository.get(meeting_id=uuid4(), owner_id="user-42"))
        self.key_for(self.meeting.id)
        await self.repository.add(self.meeting)
        self.assertIsNone(await self.repository.get(meeting_id=self.meeting.id, owner_id="other"))
        self.assertEqual(await self.repository.get(meeting_id=self.meeting.id, owner_id="user-42"), self.meeting)

    async def test_no_overwrite_and_concurrent_add(self) -> None:
        self.key_for(self.meeting.id)
        other = replace(self.meeting, title="Другая встреча")
        results = await asyncio.gather(
            self.repository.add(self.meeting), self.repository.add(other),
            return_exceptions=True,
        )
        self.assertEqual(results.count(None), 1)
        self.assertEqual(sum(isinstance(r, MeetingAlreadyExistsError) for r in results), 1)
        stored = await self.repository.get(meeting_id=self.meeting.id, owner_id="user-42")
        with self.assertRaises(MeetingAlreadyExistsError):
            await self.repository.add(replace(self.meeting, owner_id="other"))
        self.assertEqual(await self.repository.get(meeting_id=self.meeting.id, owner_id="user-42"), stored)

    async def test_task_and_meeting_keys_are_separate(self) -> None:
        self.key_for(self.meeting.id)
        task = Task(id=self.meeting.id, owner_id="user-42", title="Задача", created_at=CREATED)
        self.keys.append(f"{self.prefix}:v1:task:{task.id}")
        tasks = RedisTaskRepository(self.client, key_prefix=self.prefix)
        await tasks.add(task)
        await self.repository.add(self.meeting)
        self.assertEqual(await tasks.get(task_id=task.id, owner_id="user-42"), task)
        self.assertEqual(await self.repository.get(meeting_id=task.id, owner_id="user-42"), self.meeting)

    async def test_corrupted_records(self) -> None:
        key = self.key_for(self.meeting.id)
        await self.repository.add(self.meeting)
        good = json.loads(await self.client.get(key))
        payloads = ["broken", "[]", "{}", b"\xff"]
        for field, value in (
            ("id", "invalid"), ("id", str(uuid4())), ("owner_id", " "),
            ("title", " "), ("title", 42), ("title", " not normalized "),
            ("starts_at", END.isoformat()), ("ends_at", START.isoformat()),
        ):
            payloads.append(json.dumps({**good, field: value}))
        for field in ("starts_at", "ends_at", "created_at"):
            for value in ("invalid", "2026-10-22T08:00:00", "2026-10-22T08:00:00+03:00"):
                payloads.append(json.dumps({**good, field: value}))
        for payload in payloads:
            with self.subTest(payload=payload):
                await self.client.set(key, payload)
                with self.assertRaises(MeetingStorageError):
                    await self.repository.get(meeting_id=self.meeting.id, owner_id="user-42")

    async def test_wrong_redis_type(self) -> None:
        key = self.key_for(self.meeting.id)
        await self.client.hset(key, mapping={"unexpected": "hash"})
        with self.assertRaises(MeetingStorageError):
            await self.repository.get(meeting_id=self.meeting.id, owner_id="user-42")
