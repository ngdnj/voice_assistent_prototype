"""Task operations with explicitly supplied storage and time."""

from collections.abc import Callable
from datetime import datetime
from uuid import UUID

from app.planner.query import RecordQuery
from app.planner.repository import TaskRepository
from app.planner.task import Task, create_task


class TaskService:
    def __init__(
        self, *, repository: TaskRepository, clock: Callable[[], datetime]
    ) -> None:
        self._repository = repository
        self._clock = clock

    async def create_task(self, *, owner_id: str, title: str) -> Task:
        """Validate and store a new task for a trusted owner."""
        task = create_task(owner_id=owner_id, title=title, created_at=self._clock())
        await self._repository.add(task)
        return task

    async def get_task(self, *, task_id: UUID, owner_id: str) -> Task | None:
        """Return an accessible task without revealing other owners' records."""
        return await self._repository.get(task_id=task_id, owner_id=owner_id)

    async def list_tasks(self, query: RecordQuery) -> list[Task]:
        return await self._repository.list(query)
