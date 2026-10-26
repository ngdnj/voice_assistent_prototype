from dataclasses import replace
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.planner.meeting import Meeting
from app.planner.meeting_repository import MeetingRepository, MeetingStorageError
from app.planner.meeting_service import MeetingService

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)


def setup_service(end=NOW + timedelta(hours=2)):
    meeting = Meeting(owner_id="u", title="Хакатон", starts_at=NOW, ends_at=end, created_at=NOW)
    repo = AsyncMock(spec=MeetingRepository)
    repo.get_rescheduled.return_value = None
    repo.get.return_value = meeting
    repo.reschedule.side_effect = lambda meeting, **kwargs: meeting
    service = MeetingService(repository=repo, clock=lambda: NOW)
    return service, repo, meeting


async def test_move_preserves_duration_identity_and_creation_time():
    service, repo, original = setup_service()
    start = NOW + timedelta(days=1)
    moved = await service.reschedule_meeting(
        meeting_id=original.id, owner_id="u", starts_at=start, operation_id="move-1"
    )
    assert moved == replace(original, starts_at=start, ends_at=start + timedelta(hours=2))
    assert repo.reschedule.call_args.kwargs["changed_at"] == NOW


@pytest.mark.parametrize("mode", ["both", "end", "unknown_end"])
async def test_explicit_end_or_unknown_duration(mode):
    service, _repo, original = setup_service(None if mode == "unknown_end" else NOW + timedelta(hours=2))
    args = {"starts_at": NOW + timedelta(hours=1)} if mode != "end" else {}
    if mode != "unknown_end":
        args["ends_at"] = NOW + timedelta(hours=4)
    moved = await service.reschedule_meeting(meeting_id=original.id, owner_id="u", operation_id="m", **args)
    assert moved.starts_at == args.get("starts_at", original.starts_at)
    assert moved.ends_at == args.get("ends_at")


@pytest.mark.parametrize("args", [{}, {"starts_at": datetime(2026, 10, 27)}, {"ends_at": NOW}])
async def test_invalid_change_never_reaches_write(args):
    service, repo, original = setup_service()
    with pytest.raises(ValueError):
        await service.reschedule_meeting(meeting_id=original.id, owner_id="u", operation_id="m", **args)
    repo.reschedule.assert_not_called()


async def test_missing_or_inaccessible_meeting_is_not_updated():
    service, repo, _original = setup_service()
    repo.get.return_value = None
    assert (
        await service.reschedule_meeting(
            meeting_id=uuid4(), owner_id="other", starts_at=NOW, operation_id="m"
        )
        is None
    )
    repo.reschedule.assert_not_called()


async def test_replay_does_not_recompute_duration_or_update():
    service, repo, original = setup_service()
    repo.get_rescheduled.return_value = original
    assert (
        await service.reschedule_meeting(
            meeting_id=original.id, owner_id="u", starts_at=NOW, operation_id="m"
        )
        == original
    )
    repo.get.assert_not_called()
    repo.reschedule.assert_not_called()


async def test_storage_error_propagates():
    service, repo, original = setup_service()
    repo.reschedule.side_effect = MeetingStorageError("unavailable")
    with pytest.raises(MeetingStorageError):
        await service.reschedule_meeting(
            meeting_id=original.id, owner_id="u", starts_at=NOW, operation_id="m"
        )


async def test_reschedule_normalizes_explicit_offset_to_utc():
    service, _repo, original = setup_service()
    moved = await service.reschedule_meeting(
        meeting_id=original.id,
        owner_id="u",
        operation_id="m",
        starts_at=datetime.fromisoformat("2026-10-27T15:00:00+03:00"),
    )
    assert moved.starts_at == datetime(2026, 10, 27, 12, tzinfo=UTC)
    assert moved.starts_at.tzinfo is UTC
