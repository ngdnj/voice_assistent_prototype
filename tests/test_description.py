import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

from app.planner.fields import MAX_DESCRIPTION_LENGTH
from app.planner.meeting import create_meeting
from app.planner.meeting_repository import MeetingRepository
from app.planner.meeting_service import MeetingService
from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.reminder import create_reminder
from app.planner.reminder_repository import ReminderRepository
from app.planner.reminder_service import ReminderService
from app.planner.service import TaskService
from app.planner.task import create_task
from app.storage.memory import InMemoryTaskRepository

CREATED = datetime(2026, 10, 23, 8, tzinfo=UTC)
START = CREATED + timedelta(hours=1)
END = START + timedelta(minutes=30)


def build_all(description: str) -> list[object]:
    return [
        create_task(owner_id="user-42", title="Отчёт", created_at=CREATED, description=description),
        create_meeting(
            owner_id="user-42",
            title="Встреча",
            starts_at=START,
            ends_at=END,
            created_at=CREATED,
            description=description,
        ),
        create_reminder(
            owner_id="user-42",
            text="Позвонить",
            remind_at=START,
            created_at=CREATED,
            description=description,
        ),
    ]


class DescriptionRulesTests(unittest.TestCase):
    def test_default_is_empty(self) -> None:
        records = [
            create_task(owner_id="user-42", title="Отчёт", created_at=CREATED),
            create_meeting(
                owner_id="user-42", title="Встреча", starts_at=START, ends_at=END, created_at=CREATED
            ),
            create_reminder(owner_id="user-42", text="Позвонить", remind_at=START, created_at=CREATED),
        ]
        for record in records:
            with self.subTest(record=type(record).__name__):
                self.assertEqual(record.description, "")  # type: ignore[attr-defined]

    def test_outer_whitespace_is_stripped(self) -> None:
        for record in build_all("  Взять  ноутбук \n"):
            with self.subTest(record=type(record).__name__):
                self.assertEqual(record.description, "Взять  ноутбук")  # type: ignore[attr-defined]

    def test_length_limit(self) -> None:
        build_all("я" * MAX_DESCRIPTION_LENGTH)
        with self.assertRaisesRegex(ValueError, "description"):
            create_task(
                owner_id="user-42",
                title="Отчёт",
                created_at=CREATED,
                description="я" * (MAX_DESCRIPTION_LENGTH + 1),
            )

    def test_non_string_rejected(self) -> None:
        with self.assertRaisesRegex(TypeError, "description"):
            create_task(
                owner_id="user-42",
                title="Отчёт",
                created_at=CREATED,
                description=None,  # type: ignore[arg-type]
            )


class DescriptionOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_task_replay_and_conflict(self) -> None:
        service = TaskService(repository=InMemoryTaskRepository(), clock=lambda: CREATED)
        task = await service.create_task(
            owner_id="user-42", title="Отчёт", description="Квартальный", operation_id="op-1"
        )
        self.assertEqual(task.description, "Квартальный")
        replay = await service.create_task(
            owner_id="user-42", title="Отчёт", description=" Квартальный ", operation_id="op-1"
        )
        self.assertEqual(replay, task)
        with self.assertRaises(OperationConflictError):
            await service.create_task(
                owner_id="user-42", title="Отчёт", description="Годовой", operation_id="op-1"
            )

    async def test_empty_description_keeps_previous_fingerprints(self) -> None:
        # Operations stored before the field existed must still replay instead of conflicting.
        task_repository = AsyncMock()
        task_repository.get_created.return_value = None
        task_repository.add.side_effect = lambda record, **kwargs: record
        await TaskService(repository=task_repository, clock=lambda: CREATED).create_task(
            owner_id="user-42", title="Отчёт", operation_id="op-1"
        )
        self.assertEqual(
            task_repository.get_created.await_args.kwargs["operation"],
            CreationOperation.from_data("op-1", title="Отчёт"),
        )

        meeting_repository = AsyncMock(spec=MeetingRepository)
        meeting_repository.get_created.return_value = None
        meeting_repository.add.side_effect = lambda record, **kwargs: record
        await MeetingService(repository=meeting_repository, clock=lambda: CREATED).create_meeting(
            owner_id="user-42", title="Встреча", starts_at=START, ends_at=END, operation_id="op-2"
        )
        self.assertEqual(
            meeting_repository.get_created.await_args.kwargs["operation"],
            CreationOperation.from_data(
                "op-2", title="Встреча", starts_at=START.isoformat(), ends_at=END.isoformat()
            ),
        )

        reminder_repository = AsyncMock(spec=ReminderRepository)
        reminder_repository.get_created.return_value = None
        reminder_repository.add.side_effect = lambda record, **kwargs: record
        await ReminderService(repository=reminder_repository, clock=lambda: CREATED).create_reminder(
            owner_id="user-42", text="Позвонить", remind_at=START, operation_id="op-3"
        )
        self.assertEqual(
            reminder_repository.get_created.await_args.kwargs["operation"],
            CreationOperation.from_data("op-3", text="Позвонить", remind_at=START.isoformat()),
        )

    async def test_meeting_and_reminder_store_description(self) -> None:
        meeting_repository = AsyncMock(spec=MeetingRepository)
        meeting_repository.get_created.return_value = None
        meeting_repository.add.side_effect = lambda record, **kwargs: record
        meeting = await MeetingService(repository=meeting_repository, clock=lambda: CREATED).create_meeting(
            owner_id="user-42",
            title="Встреча",
            starts_at=START,
            ends_at=END,
            description="Обсудить бюджет",
            operation_id="op-4",
        )
        self.assertEqual(meeting.description, "Обсудить бюджет")

        reminder_repository = AsyncMock(spec=ReminderRepository)
        reminder_repository.get_created.return_value = None
        reminder_repository.add.side_effect = lambda record, **kwargs: record
        reminder_service = ReminderService(repository=reminder_repository, clock=lambda: CREATED)
        reminder = await reminder_service.create_reminder(
            owner_id="user-42",
            text="Позвонить",
            remind_at=START,
            description="Про договор",
            operation_id="op-5",
        )
        self.assertEqual(reminder.description, "Про договор")
