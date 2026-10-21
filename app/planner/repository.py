"""Storage boundary required by task operations."""

from typing import Protocol
from uuid import UUID

from app.planner.task import Task


class TaskStorageError(Exception):
    """Task storage is unavailable or contains invalid data."""


class TaskAlreadyExistsError(TaskStorageError):
    """A task ID is already stored; adding must never overwrite it."""


class TaskRepository(Protocol):
    async def add(self, task: Task) -> None:
        """Store a task before returning successfully."""
        ...

    async def get(self, *, task_id: UUID, owner_id: str) -> Task | None:
        """Return the owner's task, or None for missing or inaccessible tasks."""
        ...
