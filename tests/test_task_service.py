import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from app.planner.repository import TaskRepository
from app.planner.service import TaskService
from app.storage.memory import InMemoryTaskRepository


CREATED_AT = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)


class TaskServiceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.repository = InMemoryTaskRepository()
        self.service = TaskService(
            repository=self.repository, clock=lambda: CREATED_AT
        )

    async def test_create_and_get_task(self) -> None:
        task = await self.service.create_task(
            owner_id="user-42", title="  Подготовить отчёт  "
        )
        fetched = await self.service.get_task(task_id=task.id, owner_id="user-42")
        self.assertEqual(fetched, task)
        self.assertEqual(task.title, "Подготовить отчёт")
        self.assertEqual(task.status, "active")
        self.assertEqual(task.created_at, CREATED_AT)

    async def test_unknown_task_returns_none(self) -> None:
        self.assertIsNone(
            await self.service.get_task(task_id=uuid4(), owner_id="user-42")
        )

    async def test_other_owners_task_returns_none(self) -> None:
        task = await self.service.create_task(owner_id="user-42", title="Отчёт")
        self.assertIsNone(
            await self.service.get_task(task_id=task.id, owner_id="other-user")
        )
        self.assertEqual(
            await self.service.get_task(task_id=task.id, owner_id="user-42"), task
        )

    async def test_repository_errors_propagate(self) -> None:
        for method in ("add", "get"):
            with self.subTest(method=method):
                repository = AsyncMock(spec=TaskRepository)
                error = ConnectionError("storage unavailable")
                getattr(repository, method).side_effect = error
                service = TaskService(repository=repository, clock=lambda: CREATED_AT)
                with self.assertRaises(ConnectionError) as caught:
                    if method == "add":
                        await service.create_task(owner_id="user-42", title="Отчёт")
                    else:
                        await service.get_task(task_id=uuid4(), owner_id="user-42")
                self.assertIs(caught.exception, error)

    async def test_invalid_task_is_not_added(self) -> None:
        repository = AsyncMock(spec=TaskRepository)
        service = TaskService(repository=repository, clock=lambda: CREATED_AT)
        with self.assertRaises(ValueError):
            await service.create_task(owner_id="user-42", title=" ")
        repository.add.assert_not_awaited()

    async def test_service_works_with_another_repository(self) -> None:
        repository = AsyncMock(spec=TaskRepository)
        service = TaskService(repository=repository, clock=lambda: CREATED_AT)
        task = await service.create_task(owner_id="user-42", title="Отчёт")
        repository.add.assert_awaited_once_with(task)
        repository.get.return_value = task
        self.assertEqual(
            await service.get_task(task_id=task.id, owner_id="user-42"), task
        )
        repository.get.assert_awaited_once_with(task_id=task.id, owner_id="user-42")

    async def test_clock_is_called_for_each_creation(self) -> None:
        later = datetime(2026, 10, 21, 10, 30, tzinfo=UTC)
        moments = iter((CREATED_AT, later))
        service = TaskService(repository=self.repository, clock=lambda: next(moments))
        first = await service.create_task(owner_id="user-42", title="Первое")
        second = await service.create_task(owner_id="user-42", title="Второе")
        self.assertEqual(first.created_at, CREATED_AT)
        self.assertEqual(second.created_at, later)

    async def test_memory_repositories_are_isolated(self) -> None:
        task = await self.service.create_task(owner_id="user-42", title="Отчёт")
        other = TaskService(
            repository=InMemoryTaskRepository(), clock=lambda: CREATED_AT
        )
        self.assertIsNone(await other.get_task(task_id=task.id, owner_id="user-42"))
