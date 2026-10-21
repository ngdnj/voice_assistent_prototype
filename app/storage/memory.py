"""Demo/test storage only: tasks are lost when this instance is discarded."""

from uuid import UUID

from app.planner.task import Task


class InMemoryTaskRepository:
    """Instance-local task storage without persistence across restarts."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}

    async def add(self, task: Task) -> None:
        self._tasks[task.id] = task

    async def get(self, *, task_id: UUID, owner_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None or task.owner_id != owner_id:
            return None
        return task
