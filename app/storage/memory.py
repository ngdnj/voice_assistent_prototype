"""Demo/test storage only: tasks are lost when this instance is discarded."""

from dataclasses import replace
from uuid import UUID

from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.query import MAX_SEARCH_CANDIDATES, QueryTooBroadError, RecordQuery
from app.planner.repository import TaskAlreadyExistsError
from app.planner.task import Task, TaskStatus


class InMemoryTaskRepository:
    """Instance-local task storage without persistence across restarts."""

    def __init__(self) -> None:
        self._tasks: dict[UUID, Task] = {}
        self._operations: dict[tuple[str, str], tuple[str, Task]] = {}

    async def add(self, task: Task, *, operation: CreationOperation | None = None) -> Task:
        if operation is not None:
            previous = await self.get_created(owner_id=task.owner_id, operation=operation)
            if previous is not None:
                return previous
        if task.id in self._tasks:
            raise TaskAlreadyExistsError("Task ID already exists")
        self._tasks[task.id] = task
        if operation is not None:
            self._operations[(task.owner_id, operation.operation_id)] = (operation.fingerprint, task)
        return task

    async def get_created(self, *, owner_id: str, operation: CreationOperation) -> Task | None:
        saved = self._operations.get((owner_id, operation.operation_id))
        if saved is None:
            return None
        fingerprint, task = saved
        if fingerprint != operation.fingerprint:
            raise OperationConflictError("operation_id was already used with different data")
        # Return the current state: the status may have changed since creation.
        return self._tasks[task.id]

    async def get(self, *, task_id: UUID, owner_id: str) -> Task | None:
        task = self._tasks.get(task_id)
        if task is None or task.owner_id != owner_id:
            return None
        return task

    async def update_status(self, *, task_id: UUID, owner_id: str, status: TaskStatus) -> Task | None:
        task = await self.get(task_id=task_id, owner_id=owner_id)
        if task is None:
            return None
        updated = replace(task, status=status)
        self._tasks[task_id] = updated
        return updated

    async def list(self, query: RecordQuery) -> list[Task]:
        if query.task_period != "created_at":
            raise ValueError("In-memory task storage filters periods by created_at only")
        records = sorted(
            (
                task
                for task in self._tasks.values()
                if task.owner_id == query.owner_id and query.includes(task.created_at)
            ),
            key=lambda task: (task.created_at, task.id),
        )
        if query.query is not None:
            if len(records) > MAX_SEARCH_CANDIDATES:
                raise QueryTooBroadError("Search exceeds 1000 candidates; narrow the period")
            records = [task for task in records if query.query in task.title.casefold()]
        return records[query.offset : query.offset + query.limit]
