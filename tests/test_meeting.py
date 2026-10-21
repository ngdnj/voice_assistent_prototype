import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from app.planner.meeting import create_meeting
from app.planner.meeting_repository import MeetingRepository
from app.planner.meeting_service import MeetingService
from app.planner.time import AmbiguousLocalTimeError, NonexistentLocalTimeError

CREATED = datetime(2026, 10, 22, 8, tzinfo=UTC)
START = datetime(2026, 10, 22, 9, tzinfo=UTC)
END = datetime(2026, 10, 22, 10, tzinfo=UTC)


class MeetingTests(unittest.TestCase):
    def values(self) -> dict[str, str | datetime]:
        return dict(
            owner_id="user-42",
            title="  Обсуждение  проекта  ",
            starts_at=START,
            ends_at=END,
            created_at=CREATED,
        )

    def test_creation_and_unique_id(self) -> None:
        first = create_meeting(**self.values())
        second = create_meeting(**self.values())
        self.assertIsInstance(first.id, UUID)
        self.assertNotEqual(first.id, second.id)
        self.assertEqual(first.title, "Обсуждение  проекта")
        self.assertEqual(first.owner_id, "user-42")
        self.assertEqual((first.starts_at, first.ends_at, first.created_at), (START, END, CREATED))
        with self.assertRaises(FrozenInstanceError):
            first.ends_at = START

    def test_empty_fields(self) -> None:
        for name in ("owner_id", "title"):
            for value in ("", " \t\n"):
                with self.subTest(name=name, value=value), self.assertRaisesRegex(ValueError, name):
                    create_meeting(**{**self.values(), name: value})

    def test_all_times_must_be_aware(self) -> None:
        for name in ("starts_at", "ends_at", "created_at"):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "timezone-aware"):
                create_meeting(**{**self.values(), name: START.replace(tzinfo=None)})

    def test_end_must_follow_start(self) -> None:
        for end in (START, START - timedelta(seconds=1)):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, "ends_at"):
                create_meeting(**{**self.values(), "ends_at": end})

    def test_comparison_and_normalization_use_utc(self) -> None:
        # Wall-clock end looks earlier, but represents a later exact instant.
        meeting = create_meeting(
            owner_id="user-42",
            title="Обсуждение",
            starts_at=datetime(2026, 10, 22, 12, tzinfo=timezone(timedelta(hours=3))),
            ends_at=END,
            created_at=CREATED,
        )
        self.assertEqual(meeting.starts_at, START)
        self.assertTrue(
            all(t.tzinfo is UTC for t in (meeting.starts_at, meeting.ends_at, meeting.created_at))
        )

    def test_dst_policy_applies_to_each_time(self) -> None:
        for wall, error in (
            (datetime(2026, 3, 29, 2, 30), NonexistentLocalTimeError),
            (datetime(2026, 10, 25, 2, 30), AmbiguousLocalTimeError),
        ):
            for name in ("starts_at", "ends_at", "created_at"):
                with self.subTest(name=name, wall=wall), self.assertRaises(error):
                    create_meeting(**{**self.values(), name: wall.replace(tzinfo=ZoneInfo("Europe/Berlin"))})


class MeetingServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_create_and_get(self) -> None:
        repository = AsyncMock(spec=MeetingRepository)
        repository.get_created.return_value = None
        repository.add.side_effect = lambda record, **kwargs: record
        service = MeetingService(repository=repository, clock=lambda: CREATED)
        meeting = await service.create_meeting(
            owner_id="user-42", title="Обсуждение", starts_at=START, ends_at=END, operation_id="test-call-1"
        )
        self.assertEqual(meeting.created_at, CREATED)
        repository.add.assert_awaited_once_with(
            meeting, operation=repository.get_created.await_args.kwargs["operation"]
        )
        repository.get.return_value = meeting
        self.assertEqual(await service.get_meeting(meeting_id=meeting.id, owner_id="user-42"), meeting)
        repository.get.assert_awaited_once_with(meeting_id=meeting.id, owner_id="user-42")
        repository.get.return_value = None
        self.assertIsNone(await service.get_meeting(meeting_id=uuid4(), owner_id="user-42"))

    async def test_invalid_meeting_is_not_stored(self) -> None:
        repository = AsyncMock(spec=MeetingRepository)
        repository.get_created.return_value = None
        repository.add.side_effect = lambda record, **kwargs: record
        service = MeetingService(repository=repository, clock=lambda: CREATED)
        with self.assertRaises(ValueError):
            await service.create_meeting(
                owner_id="user-42",
                title="Обсуждение",
                starts_at=START,
                ends_at=START,
                operation_id="test-call-2",
            )
        repository.add.assert_not_awaited()

    async def test_repository_errors_propagate(self) -> None:
        for method in ("add", "get"):
            with self.subTest(method=method):
                repository = AsyncMock(spec=MeetingRepository)
                repository.get_created.return_value = None
                repository.add.side_effect = lambda record, **kwargs: record
                error = ConnectionError("storage unavailable")
                getattr(repository, method).side_effect = error
                service = MeetingService(repository=repository, clock=lambda: CREATED)
                with self.assertRaises(ConnectionError) as caught:
                    if method == "add":
                        await service.create_meeting(
                            owner_id="user-42",
                            title="Обсуждение",
                            starts_at=START,
                            ends_at=END,
                            operation_id="test-call-3",
                        )
                    else:
                        await service.get_meeting(meeting_id=uuid4(), owner_id="user-42")
                self.assertIs(caught.exception, error)
