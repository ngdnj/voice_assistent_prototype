import unittest
from dataclasses import FrozenInstanceError, fields
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.planner.reminder import PastReminderTimeError, Reminder, create_reminder
from app.planner.reminder_repository import ReminderRepository
from app.planner.reminder_service import ReminderService
from app.planner.time import AmbiguousLocalTimeError, NonexistentLocalTimeError


CREATED = datetime(2026, 10, 22, 8, tzinfo=UTC)
DUE = CREATED + timedelta(hours=1)


class ReminderTests(unittest.TestCase):
    def values(self) -> dict[str, str | datetime]:
        return dict(owner_id="user-42", text="  Позвонить  Ивану  ",
                    remind_at=DUE, created_at=CREATED)

    def test_create_normalize_and_unique_id(self) -> None:
        reminder = create_reminder(**self.values())
        self.assertIsInstance(reminder.id, UUID)
        self.assertNotEqual(reminder.id, create_reminder(**self.values()).id)
        self.assertEqual(reminder.owner_id, "user-42")
        self.assertEqual(reminder.text, "Позвонить  Ивану")
        self.assertEqual((reminder.remind_at, reminder.created_at), (DUE, CREATED))
        with self.assertRaises(FrozenInstanceError):
            setattr(reminder, "text", "")

    def test_empty_fields(self) -> None:
        for name in ("owner_id", "text"):
            for value in ("", " \t\n"):
                with self.subTest(name=name, value=value):
                    with self.assertRaisesRegex(ValueError, name):
                        create_reminder(**{**self.values(), name: value})

    def test_naive_time(self) -> None:
        for name in ("remind_at", "created_at"):
            with self.subTest(name=name):
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    create_reminder(**{**self.values(), name: DUE.replace(tzinfo=None)})

    def test_past_rejected_and_equal_allowed(self) -> None:
        with self.assertRaises(PastReminderTimeError):
            create_reminder(**{**self.values(), "remind_at": CREATED - timedelta(microseconds=1)})
        reminder = create_reminder(**{**self.values(), "remind_at": CREATED})
        self.assertEqual(reminder.remind_at, CREATED)

    def test_compare_exact_instants_and_normalize(self) -> None:
        offset = timezone(timedelta(hours=3))
        reminder = create_reminder(**{
            **self.values(), "remind_at": DUE.astimezone(offset),
            "created_at": CREATED.astimezone(offset),
        })
        self.assertEqual(reminder.remind_at, DUE)
        self.assertIs(reminder.remind_at.tzinfo, UTC)
        self.assertIs(reminder.created_at.tzinfo, UTC)

    def test_dst_policy(self) -> None:
        for wall, error in (
            (datetime(2026, 3, 29, 2, 30), NonexistentLocalTimeError),
            (datetime(2026, 10, 25, 2, 30), AmbiguousLocalTimeError),
        ):
            for name in ("remind_at", "created_at"):
                with self.subTest(name=name, wall=wall):
                    with self.assertRaises(error):
                        create_reminder(**{
                            **self.values(), name: wall.replace(tzinfo=ZoneInfo("Europe/Berlin"))
                        })

    def test_reminder_contains_no_delivery_state(self) -> None:
        self.assertEqual({f.name for f in fields(Reminder)}, {
            "id", "owner_id", "text", "remind_at", "created_at"
        })


class ReminderServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_creation_and_retrieval_after_due_time(self) -> None:
        repository = AsyncMock(spec=ReminderRepository)
        clock = Mock(return_value=CREATED)
        service = ReminderService(repository=repository, clock=clock)
        reminder = await service.create_reminder(owner_id="user-42", text="Позвонить", remind_at=DUE)
        repository.add.assert_awaited_once_with(reminder)
        clock.assert_called_once_with()
        repository.get.return_value = reminder
        clock.return_value = DUE + timedelta(days=1)
        self.assertEqual(await service.get_reminder(reminder_id=reminder.id, owner_id="user-42"), reminder)
        clock.assert_called_once_with()
        repository.get.assert_awaited_once_with(reminder_id=reminder.id, owner_id="user-42")
        repository.get.return_value = None
        self.assertIsNone(await service.get_reminder(reminder_id=uuid4(), owner_id="user-42"))

    async def test_past_not_saved(self) -> None:
        repository = AsyncMock(spec=ReminderRepository)
        service = ReminderService(repository=repository, clock=lambda: CREATED)
        with self.assertRaises(PastReminderTimeError):
            await service.create_reminder(owner_id="user-42", text="Позвонить", remind_at=CREATED - timedelta(seconds=1))
        repository.add.assert_not_awaited()

    async def test_errors_propagate(self) -> None:
        for method in ("add", "get"):
            with self.subTest(method=method):
                repository = AsyncMock(spec=ReminderRepository)
                error = ConnectionError("storage unavailable")
                getattr(repository, method).side_effect = error
                service = ReminderService(repository=repository, clock=lambda: CREATED)
                with self.assertRaises(ConnectionError) as caught:
                    if method == "add":
                        await service.create_reminder(owner_id="user-42", text="Позвонить", remind_at=DUE)
                    else:
                        await service.get_reminder(reminder_id=uuid4(), owner_id="user-42")
                self.assertIs(caught.exception, error)
