"""Task operations with explicitly supplied storage and time."""

from collections.abc import Callable
from datetime import date, datetime
from uuid import UUID

from app.planner.fields import description_fingerprint_data
from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery
from app.planner.repository import TaskRepository
from app.planner.task import TASK_STATUSES, Task, TaskStatus, create_task, normalize_due
from app.planner.time import to_utc


class TaskService:
    def __init__(self, *, repository: TaskRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    async def create_task(
        self,
        *,
        owner_id: str,
        title: str,
        operation_id: str,
        description: str = "",
        due_date: date | None = None,
        due_at: datetime | None = None,
        ends_at: datetime | None = None,
    ) -> Task:
        """Validate and store a new task for a trusted owner."""
        due_date, due_at = normalize_due(due_date, due_at)
        # Absent deadlines are omitted so operations stored before deadlines existed still replay.
        due: dict[str, str] = {}
        if due_date is not None:
            due["due_date"] = due_date.isoformat()
        if due_at is not None:
            due["due_at"] = due_at.isoformat()
        if ends_at is not None:
            ends_at = to_utc(ends_at)
            if due_at is None or ends_at <= due_at:
                raise ValueError("ends_at requires due_at and must be strictly after it")
            due["ends_at"] = ends_at.isoformat()
        operation = CreationOperation.from_data(
            operation_id, title=title.strip(), **description_fingerprint_data(description), **due
        )
        previous = await self._repository.get_created(owner_id=owner_id, operation=operation)
        if previous is not None:
            return previous
        task = create_task(
            owner_id=owner_id,
            title=title,
            created_at=self._clock(),
            description=description,
            due_date=due_date,
            due_at=due_at,
            ends_at=ends_at,
        )
        return await self._repository.add(task, operation=operation)

    async def get_task(self, *, task_id: UUID, owner_id: str) -> Task | None:
        """Return an accessible task without revealing other owners' records."""
        return await self._repository.get(task_id=task_id, owner_id=owner_id)

    async def list_tasks(self, query: RecordQuery) -> list[Task]:
        return await self._repository.list(query)

    async def update_status(self, *, task_id: UUID, owner_id: str, status: TaskStatus) -> Task | None:
        """Change the status of an accessible task; any transition is allowed, a repeat is a no-op."""
        if status not in TASK_STATUSES:
            raise ValueError("status must be one of: todo, in_progress, done")
        return await self._repository.update_status(task_id=task_id, owner_id=owner_id, status=status)
