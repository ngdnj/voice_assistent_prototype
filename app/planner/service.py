"""Task operations with explicitly supplied storage and time."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery
from app.planner.repository import TaskRepository
from app.planner.task import Task, create_task


class TaskService:
    def __init__(self, *, repository: TaskRepository, clock: Callable[[], datetime]) -> None:
        self._repository = repository
        self._clock = clock

    async def create_task(self, *, owner_id: str, title: str, operation_id: str) -> Task:
        """Validate and store a new task for a trusted owner."""
        operation = CreationOperation.from_data(operation_id, title=title.strip())
        previous = await self._repository.get_created(owner_id=owner_id, operation=operation)
        if previous is not None:
            return previous
        task = create_task(owner_id=owner_id, title=title, created_at=self._clock())
        return await self._repository.add(task, operation=operation)

    async def get_task(self, *, task_id: UUID, owner_id: str) -> Task | None:
        """Return an accessible task without revealing other owners' records."""
        return await self._repository.get(task_id=task_id, owner_id=owner_id)

    async def list_tasks(self, query: RecordQuery) -> list[Task]:
        return await self._repository.list(query)
