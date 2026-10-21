import asyncio
import unittest
from datetime import UTC, datetime, timedelta

from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.service import TaskService
from app.storage.memory import InMemoryTaskRepository

BASE = datetime(2026, 10, 22, 8, tzinfo=UTC)


class OperationTests(unittest.TestCase):
    def test_invalid_identity(self) -> None:
        for operation_id in ("", " \t", "x" * 201):
            with self.subTest(operation_id=operation_id), self.assertRaises(ValueError):
                CreationOperation.from_data(operation_id, title="Отчёт")

    def test_canonical_fingerprint(self) -> None:
        left = CreationOperation.from_data("one", title="Отчёт", value="1")
        right = CreationOperation.from_data("two", value="1", title="Отчёт")
        self.assertEqual(left.fingerprint, right.fingerprint)
        self.assertNotEqual(left.fingerprint, CreationOperation.from_data("one", title="Другое").fingerprint)


class MemoryOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_replay_ignores_new_server_time_and_preserves_result(self) -> None:
        repository = InMemoryTaskRepository()
        service = TaskService(repository=repository, clock=lambda: BASE)
        original = await service.create_task(owner_id="user-42", title=" Отчёт ", operation_id="one")
        reader = TaskService(repository=repository, clock=lambda: BASE + timedelta(days=1))
        self.assertEqual(
            await reader.create_task(owner_id="user-42", title="Отчёт", operation_id="one"), original
        )
        with self.assertRaises(OperationConflictError):
            await reader.create_task(owner_id="user-42", title="Другое", operation_id="one")

    async def test_concurrent_replay_and_owner_isolation(self) -> None:
        service = TaskService(repository=InMemoryTaskRepository(), clock=lambda: BASE)
        results = await asyncio.gather(
            *(service.create_task(owner_id="user-42", title="Отчёт", operation_id="one") for _ in range(10))
        )
        self.assertEqual(len({record.id for record in results}), 1)
        other = await service.create_task(owner_id="other", title="Отчёт", operation_id="one")
        self.assertNotEqual(other.id, results[0].id)
