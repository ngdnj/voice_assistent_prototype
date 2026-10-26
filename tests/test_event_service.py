from datetime import UTC, date, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.planner.event_repository import EventMutationRepository
from app.planner.event_service import EventService
from app.planner.meeting import Meeting
from app.planner.reminder import PastReminderTimeError, Reminder
from app.planner.task import Task

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)


def setup(record):
    repo = AsyncMock(spec=EventMutationRepository)
    repo.replay.return_value = None
    repo.get_owned.return_value = record
    repo.update.side_effect = lambda record, **kwargs: record
    repo.delete.side_effect = lambda record, **kwargs: record
    return EventService(repository=repo, clock=lambda: NOW), repo


@pytest.mark.parametrize(
    "record",
    [
        Task(owner_id="u", title="Старое", created_at=NOW, status="done"),
        Meeting(owner_id="u", title="Старое", starts_at=NOW + timedelta(days=1), created_at=NOW),
        Reminder(owner_id="u", text="Старое", remind_at=NOW + timedelta(days=1), created_at=NOW),
    ],
)
async def test_rename_preserves_identity_type_and_other_fields(record):
    service, repo = setup(record)
    changed = await service.update_event(
        event_id=record.id, owner_id="u", operation_id="rename", name=" Новое "
    )
    assert changed.id == record.id and changed.created_at == record.created_at
    assert (changed.text if isinstance(changed, Reminder) else changed.title) == "Новое"
    assert type(changed) is type(record)
    if isinstance(changed, Task):
        assert changed.status == "done"
    repo.update.assert_awaited_once()


async def test_one_update_changes_both_name_and_meeting_time_preserving_duration():
    original = Meeting(
        owner_id="u", title="Старое", starts_at=NOW, ends_at=NOW + timedelta(hours=2), created_at=NOW
    )
    service, repo = setup(original)
    changed = await service.update_event(
        event_id=original.id, owner_id="u", operation_id="up", name="Новое", moment=NOW + timedelta(days=1)
    )
    assert changed.title == "Новое" and changed.ends_at == changed.starts_at + timedelta(hours=2)
    assert repo.update.await_count == 1


async def test_task_deadline_changes_between_date_and_exact_moment_without_midnight():
    original = Task(owner_id="u", title="Отчёт", created_at=NOW, due_date=date(2026, 10, 27))
    service, repo = setup(original)
    exact = await service.update_event(
        event_id=original.id, owner_id="u", operation_id="time", moment=NOW + timedelta(days=1)
    )
    assert exact.due_date is None and exact.due_at == NOW + timedelta(days=1)
    repo.get_owned.return_value = exact
    dated = await service.update_event(
        event_id=original.id, owner_id="u", operation_id="date", day=date(2026, 10, 28)
    )
    assert dated.due_at is None and dated.due_date == date(2026, 10, 28)


@pytest.mark.parametrize(
    "changes",
    [
        {},
        {"name": "  "},
        {"name": "x" * 201},
        {"moment": datetime(2026, 10, 27)},
        {"moment": NOW, "day": date(2026, 10, 27)},
        {"end_time": NOW},
    ],
)
async def test_invalid_changes_never_reach_the_write(changes):
    original = Meeting(owner_id="u", title="Встреча", starts_at=NOW, created_at=NOW)
    service, repo = setup(original)
    with pytest.raises((ValueError, TypeError)):
        await service.update_event(event_id=original.id, owner_id="u", operation_id="bad", **changes)
    repo.update.assert_not_called()


async def test_past_reminder_can_be_renamed_but_cannot_be_rescheduled_into_the_past():
    original = Reminder(
        owner_id="u", text="Звонок", remind_at=NOW - timedelta(hours=1), created_at=NOW - timedelta(days=1)
    )
    service, repo = setup(original)
    assert await service.update_event(
        event_id=original.id, owner_id="u", operation_id="name", name="Звонок врачу"
    )
    repo.update.reset_mock()
    with pytest.raises(PastReminderTimeError):
        await service.update_event(
            event_id=original.id, owner_id="u", operation_id="time", moment=NOW - timedelta(minutes=1)
        )
    repo.update.assert_not_called()


async def test_concurrent_delete_between_replay_and_get_still_replays_its_result():
    record = Task(owner_id="u", title="Удалённая", created_at=NOW)
    service, repo = setup(None)
    repo.replay.side_effect = [None, record]
    assert await service.delete_event(event_id=record.id, owner_id="u", operation_id="delete") == record
    repo.delete.assert_not_called()


async def test_inaccessible_record_does_not_reach_the_write():
    service, repo = setup(None)
    assert (
        await service.update_event(event_id=uuid4(), owner_id="other", operation_id="edit", name="Новое")
        is None
    )
    assert await service.delete_event(event_id=uuid4(), owner_id="other", operation_id="delete") is None
    repo.update.assert_not_called()
    repo.delete.assert_not_called()
