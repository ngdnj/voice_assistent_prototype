"""Versioned JSON task storage for the Redis-backed prototype."""

import json
from dataclasses import replace
from datetime import date, datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.planner.operation import CreationOperation
from app.planner.query import QueryTooBroadError, RecordQuery
from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.task import TASK_STATUSES, Task, TaskStatus
from app.storage.redis_index import (
    ADD_INDEXED,
    INDEX_EXISTING,
    index_key,
    index_member,
    read_index,
)
from app.storage.redis_operation import commit_operation, operation_key, read_operation


# Compare-and-set: the record is replaced only if nobody changed it since it was read.
REPLACE_IF_UNCHANGED = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then return 0 end
redis.call('SET', KEYS[1], ARGV[2])
return 1
"""
MAX_STATUS_ATTEMPTS = 3


class RedisTaskRepository:
    def __init__(self, client: Redis, *, key_prefix: str = "it-gorky") -> None:
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be blank")
        self._client = client
        self._key_prefix = key_prefix

    def _key(self, task_id: UUID) -> str:
        return f"{self._key_prefix}:v1:task:{task_id}"

    @staticmethod
    def _encode(task: Task) -> str:
        data = {
            "id": str(task.id),
            "owner_id": task.owner_id,
            "title": task.title,
            "description": task.description,
            "status": task.status,
            "created_at": task.created_at.isoformat(),
        }
        # A task without a deadline stores neither field.
        if task.due_date is not None:
            data["due_date"] = task.due_date.isoformat()
        if task.due_at is not None:
            data["due_at"] = task.due_at.isoformat()
        return json.dumps(data, ensure_ascii=False)

    async def add(self, task: Task, *, operation: CreationOperation | None = None) -> Task:
        payload = self._encode(task)
        try:
            if operation is not None:
                saved = await commit_operation(
                    self._client,
                    record_key=self._key(task.id),
                    index_key=index_key(self._key_prefix, "task", task.owner_id),
                    operation_key=operation_key(
                        self._key_prefix, "task", task.owner_id, operation.operation_id
                    ),
                    operation=operation,
                    payload=payload,
                    member=index_member(task.created_at, task.id),
                )
                if saved is None:
                    raise TaskAlreadyExistsError("Task ID already exists")
                return await self._operation_result(saved, task.owner_id)
            added = await self._client.eval(
                ADD_INDEXED,
                2,
                self._key(task.id),
                index_key(self._key_prefix, "task", task.owner_id),
                payload,
                index_member(task.created_at, task.id),
            )
        except RedisError:
            raise TaskStorageError("Task storage write failed") from None
        if not added:
            raise TaskAlreadyExistsError("Task ID already exists")
        return task

    async def get(self, *, task_id: UUID, owner_id: str) -> Task | None:
        try:
            payload = await self._client.get(self._key(task_id))
        except (RedisError, UnicodeError):
            raise TaskStorageError("Task storage read failed") from None
        if payload is None:
            return None
        task = self._decode(payload, task_id)
        return task if task.owner_id == owner_id else None

    async def update_status(self, *, task_id: UUID, owner_id: str, status: TaskStatus) -> Task | None:
        try:
            for _ in range(MAX_STATUS_ATTEMPTS):
                payload = await self._client.get(self._key(task_id))
                if payload is None:
                    return None
                task = self._decode(payload, task_id)
                if task.owner_id != owner_id:
                    return None
                if task.status == status:
                    return task
                updated = replace(task, status=status)
                # Index members use created_at and ID, so the index stays valid.
                if await self._client.eval(
                    REPLACE_IF_UNCHANGED, 1, self._key(task_id), payload, self._encode(updated)
                ):
                    return updated
        except (RedisError, UnicodeError):
            raise TaskStorageError("Task status update failed") from None
        raise TaskStorageError("Task changed concurrently; retry the status update")

    @staticmethod
    def _decode(payload: str | bytes, task_id: UUID) -> Task:
        try:
            data = json.loads(payload)
            # Records written before "description" existed have no such field; deadlines are optional.
            if not isinstance(data, dict) or set(data) - {"description", "due_date", "due_at"} != {
                "id", "owner_id", "title", "status", "created_at"
            }:
                raise ValueError("Invalid fields")
            if not all(isinstance(value, str) for value in data.values()):
                raise ValueError("Invalid field types")
            stored_id = UUID(data["id"])
            # "active" was the only status before todo/in_progress/done; read it as "todo".
            status = "todo" if data["status"] == "active" else data["status"]
            if stored_id != task_id or status not in TASK_STATUSES:
                raise ValueError("Invalid identity or status")
            created_at = datetime.fromisoformat(data["created_at"])
            due_date = date.fromisoformat(data["due_date"]) if "due_date" in data else None
            due_at = datetime.fromisoformat(data["due_at"]) if "due_at" in data else None
            if created_at.utcoffset() != timedelta(0) or (
                due_at is not None and due_at.utcoffset() != timedelta(0)
            ):
                raise ValueError("Expected UTC timestamp")
            if due_date is not None and due_date.isoformat() != data["due_date"]:
                raise ValueError("Expected ISO date")
            task = Task(
                id=stored_id,
                owner_id=data["owner_id"],
                title=data["title"],
                description=data.get("description", ""),
                status=status,
                created_at=created_at,
                due_date=due_date,
                due_at=due_at,
            )
            if task.title != data["title"] or task.description != data.get("description", ""):
                raise ValueError("Expected normalized title and description")
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
                if (
                    record.owner_id != query.owner_id
                    or index_member(record.created_at, record.id) != member
                    or not query.includes(record.created_at)
                ):
                    raise ValueError("Index does not match stored record")
                if query.query is None or query.query in record.title.casefold():
                    records.append(record)
            if query.query is not None:
                return records[query.offset : query.offset + query.limit]
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
                    INDEX_EXISTING,
                    2,
                    key,
                    index_key(self._key_prefix, "task", record.owner_id),
                    payload,
                    index_member(record.created_at, record.id),
                )
                count += 1
            return count
        except (RedisError, UnicodeError, ValueError):
            raise TaskStorageError("Task index backfill failed") from None

    async def get_created(self, *, owner_id: str, operation: CreationOperation) -> Task | None:
        try:
            saved = await read_operation(
                self._client,
                operation_key(self._key_prefix, "task", owner_id, operation.operation_id),
                operation,
            )
            return None if saved is None else await self._operation_result(saved, owner_id)
        except (RedisError, UnicodeError):
            raise TaskStorageError("Task operation read failed") from None

    async def _operation_result(self, payload: str, owner_id: str) -> Task:
        try:
            record = self._decode(payload, UUID(json.loads(payload)["id"]))
            if record.owner_id != owner_id:
                raise ValueError("Operation owner mismatch")
            actual = await self._client.get(self._key(record.id))
            score = await self._client.zscore(
                index_key(self._key_prefix, "task", owner_id), index_member(record.created_at, record.id)
            )
            if actual is None or score != 0:
                raise ValueError("Operation result is inconsistent with record/index")
            current = self._decode(actual, record.id)
            # Only the status may change after creation; return the current state.
            if replace(current, status=record.status) != record:
                raise ValueError("Operation result is inconsistent with record")
            return current
        except (RedisError, UnicodeError, ValueError, TypeError, KeyError):
            raise TaskStorageError("Task operation result is invalid") from None
