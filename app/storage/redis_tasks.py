"""Versioned JSON task storage for the Redis-backed prototype."""

import json
from datetime import datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.planner.query import QueryTooBroadError, RecordQuery
from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.task import Task
from app.storage.redis_index import (
    ADD_INDEXED, INDEX_EXISTING, index_key, index_member, read_index,
)


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
            added = await self._client.eval(
                ADD_INDEXED, 2, self._key(task.id),
                index_key(self._key_prefix, "task", task.owner_id),
                payload, index_member(task.created_at, task.id),
            )
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

    async def list(self, query: RecordQuery) -> list[Task]:
        try:
            members = await read_index(
                self._client, index_key(self._key_prefix, "task", query.owner_id), query
            )
            if not members:
                return []
            ids = [UUID(member.rsplit("|", 1)[1]) for member in members]
            payloads = await self._client.mget([self._key(record_id) for record_id in ids])
            records: list[Task] = []
            for record_id, member, payload in zip(ids, members, payloads, strict=True):
                if payload is None:
                    raise ValueError("Index points to a missing record")
                record = self._decode(payload, record_id)
                if (record.owner_id != query.owner_id
                        or index_member(record.created_at, record.id) != member
                        or not query.includes(record.created_at)):
                    raise ValueError("Index does not match stored record")
                if query.query is None or query.query in record.title.casefold():
                    records.append(record)
            if query.query is not None:
                return records[query.offset:query.offset + query.limit]
            return records
        except QueryTooBroadError:
            raise
        except (RedisError, UnicodeError, ValueError, IndexError):
            raise TaskStorageError("Task index read failed") from None

    async def reindex(self) -> int:
        """Explicit offline backfill for legacy records; never called by list/get."""
        count = 0
        try:
            async for key in self._client.scan_iter(match=f"{self._key_prefix}:v1:task:*", count=100):
                record_id = UUID(key.rsplit(":", 1)[1])
                payload = await self._client.get(key)
                if payload is None:
                    continue
                record = self._decode(payload, record_id)
                await self._client.eval(
                    INDEX_EXISTING, 2, key,
                    index_key(self._key_prefix, "task", record.owner_id),
                    payload, index_member(record.created_at, record.id),
                )
                count += 1
            return count
        except (RedisError, UnicodeError, ValueError):
            raise TaskStorageError("Task index backfill failed") from None
