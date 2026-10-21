import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID

from app.planner.meeting_repository import MeetingRepository
from app.planner.meeting_service import MeetingService
from app.planner.query import RecordQuery
from app.planner.reminder_repository import ReminderRepository
from app.planner.reminder_service import ReminderService
from app.planner.repository import TaskRepository
from app.planner.service import TaskService
from app.planner.task import Task
from app.storage.memory import InMemoryTaskRepository


BASE = datetime(2026, 10, 22, 8, tzinfo=UTC)


class RecordQueryTests(unittest.TestCase):
    def test_validation(self) -> None:
        for values in (
            {"owner_id": " "}, {"limit": 0}, {"limit": 101}, {"limit": True},
            {"offset": -1}, {"offset": 10001}, {"offset": 1.5},
            {"query": " "}, {"query": "x" * 201},
            {"start_time": BASE, "end_time": BASE},
            {"start_time": BASE + timedelta(seconds=1), "end_time": BASE},
            {"start_time": BASE.replace(tzinfo=None)},
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    RecordQuery(**{"owner_id": "user-42", **values})


class RecordServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_services_delegate_without_changing_query(self) -> None:
        query = RecordQuery(owner_id="user-42", query="отчёт")
        for protocol, service_type, method in (
            (TaskRepository, TaskService, "list_tasks"),
            (MeetingRepository, MeetingService, "list_meetings"),
            (ReminderRepository, ReminderService, "list_reminders"),
        ):
            with self.subTest(method=method):
                repository = AsyncMock(spec=protocol)
                repository.list.return_value = []
                service = service_type(repository=repository, clock=lambda: BASE)
                self.assertEqual(await getattr(service, method)(query), [])
                repository.list.assert_awaited_once_with(query)

    async def test_memory_listing_search_and_owner_filter(self) -> None:
        repository = InMemoryTaskRepository()
        service = TaskService(repository=repository, clock=lambda: BASE)
        for record_id, owner, title in (
            (3, "user-42", "Третий"), (2, "other", "Отчёт"),
            (1, "user-42", "ОтЧёТ"), (4, "user-42", "Другой отчёт"),
        ):
            await repository.add(Task(
                id=UUID(int=record_id), owner_id=owner, title=title, created_at=BASE
            ))
        self.assertEqual([r.id.int for r in await service.list_tasks(RecordQuery(
            owner_id="user-42", limit=2, offset=1
        ))], [3, 4])
        self.assertEqual([r.id.int for r in await service.list_tasks(RecordQuery(
            owner_id="user-42", query=" ОТЧЁТ "
        ))], [1, 4])
        self.assertEqual(await service.list_tasks(RecordQuery(
            owner_id="user-42", end_time=BASE
        )), [])
