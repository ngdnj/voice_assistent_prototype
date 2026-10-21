"""Versioned JSON task storage for the Redis-backed prototype."""

import json
from datetime import datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.task import Task


class RedisTaskRepository:
    def __init__(self, client: Redis, *, key_prefix: str = "it-gorky") -> None:
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be blank")
        self._client = client
        self._key_prefix = key_prefix

    def _key(self, task_id: UUID) -> str:
        return f"{self._key_prefix}:v1:task:{task_id}"

    async def add(self, task: Task) -> None:
        payload = json.dumps({
            "id": str(task.id),
            "owner_id": task.owner_id,
            "title": task.title,
            "status": task.status,
            "created_at": task.created_at.isoformat(),
        }, ensure_ascii=False)
        try:
            added = await self._client.set(self._key(task.id), payload, nx=True)
        except RedisError:
            raise TaskStorageError("Task storage write failed") from None
        if not added:
            raise TaskAlreadyExistsError("Task ID already exists")

    async def get(self, *, task_id: UUID, owner_id: str) -> Task | None:
        try:
            payload = await self._client.get(self._key(task_id))
        except (RedisError, UnicodeError):
            raise TaskStorageError("Task storage read failed") from None
        if payload is None:
            return None
        task = self._decode(payload, task_id)
        return task if task.owner_id == owner_id else None

    @staticmethod
    def _decode(payload: str | bytes, task_id: UUID) -> Task:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or set(data) != {
                "id", "owner_id", "title", "status", "created_at"
            }:
                raise ValueError("Invalid fields")
            if not all(isinstance(value, str) for value in data.values()):
                raise ValueError("Invalid field types")
            stored_id = UUID(data["id"])
            if stored_id != task_id or data["status"] != "active":
                raise ValueError("Invalid identity or status")
            created_at = datetime.fromisoformat(data["created_at"])
            if created_at.utcoffset() != timedelta(0):
                raise ValueError("Expected UTC timestamp")
            task = Task(
                id=stored_id, owner_id=data["owner_id"],
                title=data["title"], created_at=created_at,
            )
            if task.title != data["title"]:
                raise ValueError("Expected normalized title")
            return task
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise TaskStorageError("Stored task data is invalid") from None
