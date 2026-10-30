from datetime import UTC, date, datetime, timedelta

import pytest

from app.planner.operation import OperationConflictError
from app.planner.service import TaskService
from app.planner.task import create_task
from app.storage.memory import InMemoryTaskRepository
from app.storage.redis_tasks import RedisTaskRepository

NOW = datetime(2026, 10, 30, 9, tzinfo=UTC)


def test_task_interval_is_explicit_and_normalized_to_utc():
    task = create_task(
        owner_id="owner",
        title="Работа",
        created_at=NOW,
        due_at=datetime.fromisoformat("2026-10-30T15:00:00+03:00"),
        ends_at=datetime.fromisoformat("2026-10-30T17:00:00+03:00"),
    )
    assert task.due_at == NOW + timedelta(hours=3)
    assert task.ends_at == NOW + timedelta(hours=5)
    assert task.due_date is None
    assert RedisTaskRepository._decode(RedisTaskRepository._encode(task), task.id) == task


@pytest.mark.parametrize(
    "values",
    [
        {"ends_at": NOW},
        {"due_date": date(2026, 10, 30), "ends_at": NOW},
        {"due_at": NOW, "ends_at": NOW},
        {"due_at": NOW, "ends_at": NOW - timedelta(seconds=1)},
        {"due_at": NOW, "ends_at": datetime(2026, 10, 30, 12)},
    ],
)
def test_invalid_task_intervals_are_rejected(values):
    with pytest.raises(ValueError):
        create_task(owner_id="owner", title="Работа", created_at=NOW, **values)


async def test_task_range_survives_service_replay_and_conflicting_end_is_rejected():
    repo = InMemoryTaskRepository()
    service = TaskService(repository=repo, clock=lambda: NOW)
    values = dict(
        owner_id="owner", title="Работа", operation_id="range-1", due_at=NOW, ends_at=NOW + timedelta(hours=1)
    )
    task = await service.create_task(**values)
    assert await service.get_task(task_id=task.id, owner_id="owner") == task
    assert await service.create_task(**values) == task
    with pytest.raises(OperationConflictError):
        await service.create_task(**{**values, "ends_at": NOW + timedelta(hours=2)})
