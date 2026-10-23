import asyncio
import json
import os
import unittest
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

from redis.exceptions import ConnectionError

from app.planner.reminder import create_reminder
from app.planner.reminder_repository import ReminderAlreadyExistsError, ReminderStorageError
from app.planner.reminder_service import ReminderService
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_index import index_key
from app.storage.redis_reminders import RedisReminderRepository

CREATED = datetime(2026, 10, 21, 8, 0, 0, 123456, tzinfo=UTC)
DUE = CREATED + timedelta(hours=1)


class RedisReminderErrorTests(unittest.IsolatedAsyncioTestCase):
    async def test_infrastructure_errors(self) -> None:
        for method in ("eval", "get"):
            with self.subTest(method=method):
                client = AsyncMock()
                getattr(client, method).side_effect = ConnectionError("private data")
                repository = RedisReminderRepository(client)
                with self.assertRaises(ReminderStorageError) as caught:
                    if method == "eval":
                        await repository.add(
                            create_reminder(
                                owner_id="user-42", text="Позвонить", remind_at=DUE, created_at=CREATED
                            )
                        )
                    else:
                        await repository.get(reminder_id=uuid4(), owner_id="user-42")
                self.assertNotIn("private data", str(caught.exception))


@unittest.skipUnless(
    os.environ.get("RUN_REDIS_INTEGRATION") == "1",
    "set RUN_REDIS_INTEGRATION=1 for a dedicated test Redis",
)
class RedisReminderIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.connection = redis_connection(RedisSettings.from_env())
        self.client = await self.connection.__aenter__()
        self.addAsyncCleanup(self.connection.__aexit__, None, None, None)
        self.prefix = f"it-gorky:test:{uuid4()}"
        self.keys: list[str] = []
        self.addAsyncCleanup(self.cleanup_keys)
        self.repository = RedisReminderRepository(self.client, key_prefix=self.prefix)
        self.reminder = create_reminder(
            owner_id="user-42", text="Позвонить 📞", remind_at=DUE, created_at=CREATED
        )

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

    def key_for(self, reminder_id: UUID) -> str:
        key = f"{self.prefix}:v1:reminder:{reminder_id}"
        self.keys.append(key)
        return key

    async def test_service_roundtrip_after_due_with_new_client(self) -> None:
        service = ReminderService(repository=self.repository, clock=lambda: CREATED)
        reminder = await service.create_reminder(
            owner_id="user-42", text="Позвонить 📞", remind_at=DUE, operation_id="test-call-1"
        )
        key = self.key_for(reminder.id)
        async with redis_connection(RedisSettings.from_env()) as client:
            reader = ReminderService(
                repository=RedisReminderRepository(client, key_prefix=self.prefix),
                clock=lambda: DUE + timedelta(days=1),
            )
            loaded = await reader.get_reminder(reminder_id=reminder.id, owner_id="user-42")
        self.assertEqual(loaded, reminder)
        self.assertEqual(await self.client.ttl(key), -1)
        self.assertEqual(
            json.loads(await self.client.get(key)),
            {
                "id": str(reminder.id),
                "owner_id": "user-42",
                "text": "Позвонить 📞",
                "description": "",
                "remind_at": DUE.isoformat(),
                "created_at": CREATED.isoformat(),
            },
        )

    async def test_missing_and_foreign(self) -> None:
        self.assertIsNone(await self.repository.get(reminder_id=uuid4(), owner_id="user-42"))
        self.key_for(self.reminder.id)
        await self.repository.add(self.reminder)
        self.assertIsNone(await self.repository.get(reminder_id=self.reminder.id, owner_id="other"))
        self.assertEqual(
            await self.repository.get(reminder_id=self.reminder.id, owner_id="user-42"), self.reminder
        )

    async def test_atomic_no_overwrite(self) -> None:
        self.key_for(self.reminder.id)
        other = replace(self.reminder, text="Другой текст")
        results = await asyncio.gather(
            self.repository.add(self.reminder),
            self.repository.add(other),
            return_exceptions=True,
        )
        self.assertEqual(sum(not isinstance(r, BaseException) for r in results), 1)
        self.assertEqual(sum(isinstance(r, ReminderAlreadyExistsError) for r in results), 1)
        stored = await self.repository.get(reminder_id=self.reminder.id, owner_id="user-42")
        with self.assertRaises(ReminderAlreadyExistsError):
            await self.repository.add(replace(self.reminder, owner_id="other"))
        self.assertEqual(await self.repository.get(reminder_id=self.reminder.id, owner_id="user-42"), stored)

    async def test_corrupt_data(self) -> None:
        key = self.key_for(self.reminder.id)
        await self.repository.add(self.reminder)
        good = json.loads(await self.client.get(key))
        payloads = ["broken", "[]", "{}", b"\xff"]
        for field, value in (
            ("id", "invalid"),
            ("id", str(uuid4())),
            ("owner_id", " "),
            ("text", " "),
            ("text", 42),
            ("text", " not normalized "),
            ("description", " not normalized "),
            ("description", 42),
            ("remind_at", (CREATED - timedelta(seconds=1)).isoformat()),
        ):
            payloads.append(json.dumps({**good, field: value}))
        for field in ("remind_at", "created_at"):
            for value in ("invalid", "2026-10-21T08:00:00", "2026-10-21T08:00:00+03:00"):
                payloads.append(json.dumps({**good, field: value}))
        # Delivery state is not part of the stored reminder schema.
        payloads.append(json.dumps({**good, "sent": True}))
        for payload in payloads:
            with self.subTest(payload=payload):
                await self.client.set(key, payload)
                with self.assertRaises(ReminderStorageError):
                    await self.repository.get(reminder_id=self.reminder.id, owner_id="user-42")

    async def test_legacy_record_without_description(self) -> None:
        key = self.key_for(self.reminder.id)
        await self.repository.add(self.reminder)
        legacy = json.loads(await self.client.get(key))
        del legacy["description"]
        await self.client.set(key, json.dumps(legacy))
        self.assertEqual(
            await self.repository.get(reminder_id=self.reminder.id, owner_id="user-42"), self.reminder
        )

    async def test_wrong_redis_type(self) -> None:
        key = self.key_for(self.reminder.id)
        await self.client.hset(key, mapping={"unexpected": "hash"})
        with self.assertRaises(ReminderStorageError):
            await self.repository.get(reminder_id=self.reminder.id, owner_id="user-42")
