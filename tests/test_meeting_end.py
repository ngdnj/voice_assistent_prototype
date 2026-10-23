import unittest
from datetime import UTC, datetime, timedelta, timezone
from unittest.mock import AsyncMock

from app.planner.meeting import create_meeting
from app.planner.meeting_repository import MeetingRepository
from app.planner.meeting_service import MeetingService
from app.planner.operation import CreationOperation

CREATED = datetime(2026, 10, 23, 8, tzinfo=UTC)
START = CREATED + timedelta(hours=1)
MSK = timezone(timedelta(hours=3))


def recording_repository() -> AsyncMock:
    repository = AsyncMock(spec=MeetingRepository)
    repository.get_created.return_value = None
    repository.add.side_effect = lambda record, **kwargs: record
    return repository


class MeetingEndTests(unittest.TestCase):
    def test_end_is_optional_and_not_invented(self) -> None:
        meeting = create_meeting(owner_id="user-42", title="Встреча", starts_at=START, created_at=CREATED)
        self.assertIsNone(meeting.ends_at)

    def test_given_end_is_utc_and_after_start(self) -> None:
        meeting = create_meeting(
            owner_id="user-42",
            title="Встреча",
            starts_at=START,
            ends_at=datetime(2026, 10, 23, 12, 30, tzinfo=MSK),
            created_at=CREATED,
        )
        self.assertEqual(meeting.ends_at, datetime(2026, 10, 23, 9, 30, tzinfo=UTC))
        for end in (START, START - timedelta(minutes=1)):
            with self.subTest(end=end), self.assertRaisesRegex(ValueError, "ends_at"):
                create_meeting(
                    owner_id="user-42", title="Встреча", starts_at=START, ends_at=end, created_at=CREATED
                )


class MeetingEndOperationTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_end_is_not_in_fingerprint(self) -> None:
        repository = recording_repository()
        meeting = await MeetingService(repository=repository, clock=lambda: CREATED).create_meeting(
            owner_id="user-42", title="Встреча", starts_at=START, operation_id="op-1"
        )
        self.assertIsNone(meeting.ends_at)
        self.assertEqual(
            repository.get_created.await_args.kwargs["operation"],
            CreationOperation.from_data("op-1", title="Встреча", starts_at=START.isoformat()),
        )

    async def test_end_changes_fingerprint(self) -> None:
        fingerprints = []
        for end in (None, START + timedelta(minutes=30), START + timedelta(hours=1)):
            repository = recording_repository()
            await MeetingService(repository=repository, clock=lambda: CREATED).create_meeting(
                owner_id="user-42", title="Встреча", starts_at=START, ends_at=end, operation_id="op-2"
            )
            fingerprints.append(repository.get_created.await_args.kwargs["operation"].fingerprint)
        self.assertEqual(len(set(fingerprints)), 3)
