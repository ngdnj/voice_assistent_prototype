"""Demo/test storage only: tasks are lost when this instance is discarded."""

from uuid import UUID

from app.planner.query import MAX_SEARCH_CANDIDATES, QueryTooBroadError, RecordQuery
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

    async def list(self, query: RecordQuery) -> list[Task]:
        records = sorted(
            (task for task in self._tasks.values()
             if task.owner_id == query.owner_id and query.includes(task.created_at)),
            key=lambda task: (task.created_at, task.id),
        )
        if query.query is not None:
            if len(records) > MAX_SEARCH_CANDIDATES:
                raise QueryTooBroadError("Search exceeds 1000 candidates; narrow the period")
            records = [task for task in records if query.query in task.title.casefold()]
        return records[query.offset:query.offset + query.limit]
