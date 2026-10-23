"""Storage boundary required by task operations."""

from typing import Protocol
from uuid import UUID

from app.planner.operation import CreationOperation
from app.planner.query import RecordQuery
from app.planner.task import Task, TaskStatus


class TaskStorageError(Exception):
    """Task storage is unavailable or contains invalid data."""


class TaskAlreadyExistsError(TaskStorageError):
    """A task ID is already stored; adding must never overwrite it."""


class TaskRepository(Protocol):
    async def add(self, task: Task, *, operation: CreationOperation | None = None) -> Task:
        """Store a task before returning successfully."""
        ...

    async def get(self, *, task_id: UUID, owner_id: str) -> Task | None:
        """Return the owner's task, or None for missing or inaccessible tasks."""
        ...

    async def list(self, query: RecordQuery) -> list[Task]:
        """List or search accessible records in stable time/ID order."""
        ...

    async def get_created(self, *, owner_id: str, operation: CreationOperation) -> Task | None:
        """Replay a matching creation or raise OperationConflictError."""
        ...

    async def update_status(self, *, task_id: UUID, owner_id: str, status: TaskStatus) -> Task | None:
        """Store a new status of the owner's task; None for missing or inaccessible tasks."""
        ...
