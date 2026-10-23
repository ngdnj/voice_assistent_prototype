import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

from app.planner.repository import TaskRepository
from app.planner.service import TaskService
from app.storage.memory import InMemoryTaskRepository

CREATED = datetime(2026, 10, 23, 8, tzinfo=UTC)


class TaskStatusUpdateTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = TaskService(repository=InMemoryTaskRepository(), clock=lambda: CREATED)

    async def test_any_transition_is_stored(self) -> None:
        task = await self.service.create_task(owner_id="user-42", title="Отчёт", operation_id="op-1")
        for status in ("in_progress", "done", "todo", "done"):
            with self.subTest(status=status):
                updated = await self.service.update_status(task_id=task.id, owner_id="user-42", status=status)
                assert updated is not None
                self.assertEqual(updated.status, status)
                self.assertEqual(await self.service.get_task(task_id=task.id, owner_id="user-42"), updated)
                self.assertEqual(
                    (updated.id, updated.title, updated.created_at), (task.id, task.title, CREATED)
                )

    async def test_repeat_is_a_no_op(self) -> None:
        task = await self.service.create_task(owner_id="user-42", title="Отчёт", operation_id="op-1")
        first = await self.service.update_status(task_id=task.id, owner_id="user-42", status="done")
        second = await self.service.update_status(task_id=task.id, owner_id="user-42", status="done")
        self.assertEqual(first, second)

    async def test_missing_and_foreign_tasks_give_none(self) -> None:
        task = await self.service.create_task(owner_id="user-42", title="Отчёт", operation_id="op-1")
        self.assertIsNone(
            await self.service.update_status(task_id=uuid4(), owner_id="user-42", status="done")
        )
        self.assertIsNone(await self.service.update_status(task_id=task.id, owner_id="other", status="done"))
        unchanged = await self.service.get_task(task_id=task.id, owner_id="user-42")
        assert unchanged is not None
        self.assertEqual(unchanged.status, "todo")

    async def test_unknown_status_is_rejected_before_storage(self) -> None:
        repository = AsyncMock(spec=TaskRepository)
        service = TaskService(repository=repository, clock=lambda: CREATED)
        for status in ("active", "DONE", "", "cancelled"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "status"):
                await service.update_status(
                    task_id=uuid4(),
                    owner_id="user-42",
                    status=status,  # type: ignore[arg-type]
                )
        repository.update_status.assert_not_awaited()

    async def test_create_replay_after_status_change_returns_current_task(self) -> None:
        task = await self.service.create_task(owner_id="user-42", title="Отчёт", operation_id="op-1")
        await self.service.update_status(task_id=task.id, owner_id="user-42", status="done")
        replay = await self.service.create_task(owner_id="user-42", title="Отчёт", operation_id="op-1")
        self.assertEqual((replay.id, replay.status), (task.id, "done"))
