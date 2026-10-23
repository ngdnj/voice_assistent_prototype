import unittest
from datetime import UTC, date, datetime, timedelta, timezone

from app.planner.operation import OperationConflictError
from app.planner.service import TaskService
from app.planner.task import create_task
from app.storage.memory import InMemoryTaskRepository

CREATED = datetime(2026, 10, 23, 8, tzinfo=UTC)
MSK = timezone(timedelta(hours=3))


class TaskDueTests(unittest.TestCase):
    def test_no_deadline_by_default(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED)
        self.assertIsNone(task.due_date)
        self.assertIsNone(task.due_at)

    def test_date_only_deadline_stays_a_date(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED, due_date=date(2026, 10, 24))
        self.assertEqual(task.due_date, date(2026, 10, 24))
        self.assertNotIsInstance(task.due_date, datetime)
        self.assertIsNone(task.due_at)

    def test_exact_deadline_is_stored_in_utc(self) -> None:
        task = create_task(
            owner_id="user-42",
            title="Отчёт",
            created_at=CREATED,
            due_at=datetime(2026, 10, 24, 18, tzinfo=MSK),
        )
        self.assertEqual(task.due_at, datetime(2026, 10, 24, 15, tzinfo=UTC))
        self.assertIs(task.due_at.tzinfo, UTC)  # type: ignore[union-attr]

    def test_date_and_datetime_are_mutually_exclusive(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            create_task(
                owner_id="user-42",
                title="Отчёт",
                created_at=CREATED,
                due_date=date(2026, 10, 24),
                due_at=datetime(2026, 10, 24, 18, tzinfo=UTC),
            )

    def test_invalid_deadline_types(self) -> None:
        with self.assertRaises(TypeError):
            create_task(
                owner_id="user-42",
                title="Отчёт",
                created_at=CREATED,
                due_date=datetime(2026, 10, 24, tzinfo=UTC),
            )
        with self.assertRaises(TypeError):
            create_task(
                owner_id="user-42",
                title="Отчёт",
                created_at=CREATED,
                due_at=date(2026, 10, 24),  # type: ignore[arg-type]
            )
        with self.assertRaisesRegex(ValueError, "aware"):
            create_task(
                owner_id="user-42", title="Отчёт", created_at=CREATED, due_at=datetime(2026, 10, 24, 18)
            )


class TaskDueOperationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = TaskService(repository=InMemoryTaskRepository(), clock=lambda: CREATED)

    async def test_replay_with_same_deadline(self) -> None:
        task = await self.service.create_task(
            owner_id="user-42",
            title="Отчёт",
            due_at=datetime(2026, 10, 24, 18, tzinfo=MSK),
            operation_id="op-1",
        )
        # The same instant expressed in UTC is the same operation.
        replay = await self.service.create_task(
            owner_id="user-42",
            title="Отчёт",
            due_at=datetime(2026, 10, 24, 15, tzinfo=UTC),
            operation_id="op-1",
        )
        self.assertEqual(replay, task)

    async def test_changed_deadline_conflicts(self) -> None:
        await self.service.create_task(
            owner_id="user-42", title="Отчёт", due_date=date(2026, 10, 24), operation_id="op-2"
        )
        for changes in (
            {"due_date": date(2026, 10, 25)},
            {"due_at": datetime(2026, 10, 24, 18, tzinfo=UTC)},
            {},
        ):
            with self.subTest(changes=changes), self.assertRaises(OperationConflictError):
                await self.service.create_task(
                    owner_id="user-42",
                    title="Отчёт",
                    operation_id="op-2",
                    **changes,  # type: ignore[arg-type]
                )

    async def test_invalid_deadline_is_rejected_before_storage(self) -> None:
        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            await self.service.create_task(
                owner_id="user-42",
                title="Отчёт",
                due_date=date(2026, 10, 24),
                due_at=datetime(2026, 10, 24, 18, tzinfo=UTC),
                operation_id="op-3",
            )
