"""Versioned JSON storage for prototype meetings, without expiration."""

import json
from datetime import datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.planner.meeting import Meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError, MeetingStorageError
from app.planner.operation import CreationOperation
from app.planner.query import QueryTooBroadError, RecordQuery
from app.storage.redis_index import (
    ADD_INDEXED,
    INDEX_EXISTING,
    index_key,
    index_member,
    read_index,
)
from app.storage.redis_operation import commit_operation, operation_key, read_operation


class RedisMeetingRepository:
    def __init__(self, client: Redis, *, key_prefix: str = "it-gorky") -> None:
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be blank")
        self._client = client
        self._key_prefix = key_prefix

    def _key(self, meeting_id: UUID) -> str:
        return f"{self._key_prefix}:v1:meeting:{meeting_id}"

    async def add(self, meeting: Meeting, *, operation: CreationOperation | None = None) -> Meeting:
        payload = json.dumps(
            {
                "id": str(meeting.id),
                "owner_id": meeting.owner_id,
                "title": meeting.title,
                "starts_at": meeting.starts_at.isoformat(),
                "ends_at": meeting.ends_at.isoformat(),
                "created_at": meeting.created_at.isoformat(),
            },
            ensure_ascii=False,
        )
        try:
            if operation is not None:
                saved = await commit_operation(
                    self._client,
                    record_key=self._key(meeting.id),
                    index_key=index_key(self._key_prefix, "meeting", meeting.owner_id),
                    operation_key=operation_key(
                        self._key_prefix, "meeting", meeting.owner_id, operation.operation_id
                    ),
                    operation=operation,
                    payload=payload,
                    member=index_member(meeting.starts_at, meeting.id),
                )
                if saved is None:
                    raise MeetingAlreadyExistsError("Meeting ID already exists")
                return await self._operation_result(saved, meeting.owner_id)
            added = await self._client.eval(
                ADD_INDEXED,
                2,
                self._key(meeting.id),
                index_key(self._key_prefix, "meeting", meeting.owner_id),
                payload,
                index_member(meeting.starts_at, meeting.id),
            )
        except RedisError:
            raise MeetingStorageError("Meeting storage write failed") from None
        if not added:
            raise MeetingAlreadyExistsError("Meeting ID already exists")
        return meeting

    async def get(self, *, meeting_id: UUID, owner_id: str) -> Meeting | None:
        try:
            payload = await self._client.get(self._key(meeting_id))
        except (RedisError, UnicodeError):
            raise MeetingStorageError("Meeting storage read failed") from None
        if payload is None:
            return None
        meeting = self._decode(payload, meeting_id)
        return meeting if meeting.owner_id == owner_id else None

    @staticmethod
    def _decode(payload: str | bytes, meeting_id: UUID) -> Meeting:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or set(data) != {
                "id",
                "owner_id",
                "title",
                "starts_at",
                "ends_at",
                "created_at",
            }:
                raise ValueError("Invalid fields")
            if not all(isinstance(value, str) for value in data.values()):
                raise ValueError("Invalid field types")
            stored_id = UUID(data["id"])
            if stored_id != meeting_id:
                raise ValueError("Invalid identity")
            moments = {
                name: datetime.fromisoformat(data[name]) for name in ("starts_at", "ends_at", "created_at")
            }
            if any(value.utcoffset() != timedelta(0) for value in moments.values()):
                raise ValueError("Expected UTC timestamps")
            meeting = Meeting(id=stored_id, owner_id=data["owner_id"], title=data["title"], **moments)
            if meeting.title != data["title"]:
                raise ValueError("Expected normalized title")
            return meeting
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MeetingStorageError("Stored meeting data is invalid") from None

    async def list(self, query: RecordQuery) -> list[Meeting]:
        try:
            members = await read_index(
                self._client, index_key(self._key_prefix, "meeting", query.owner_id), query
            )
            if not members:
                return []
            ids = [UUID(member.rsplit("|", 1)[1]) for member in members]
            payloads = await self._client.mget([self._key(record_id) for record_id in ids])
            records: list[Meeting] = []
            for record_id, member, payload in zip(ids, members, payloads, strict=True):
                if payload is None:
                    raise ValueError("Index points to a missing record")
                record = self._decode(payload, record_id)
                if (
                    record.owner_id != query.owner_id
                    or index_member(record.starts_at, record.id) != member
                    or not query.includes(record.starts_at)
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
            raise MeetingStorageError("Meeting index read failed") from None

    async def reindex(self) -> int:
        """Explicit offline backfill for legacy records; never called by list/get."""
        count = 0
        try:
            async for key in self._client.scan_iter(match=f"{self._key_prefix}:v1:meeting:*", count=100):
                record_id = UUID(key.rsplit(":", 1)[1])
                payload = await self._client.get(key)
                if payload is None:
                    continue
                record = self._decode(payload, record_id)
                await self._client.eval(
                    INDEX_EXISTING,
                    2,
                    key,
                    index_key(self._key_prefix, "meeting", record.owner_id),
                    payload,
                    index_member(record.starts_at, record.id),
                )
                count += 1
            return count
        except (RedisError, UnicodeError, ValueError):
            raise MeetingStorageError("Meeting index backfill failed") from None

    async def get_created(self, *, owner_id: str, operation: CreationOperation) -> Meeting | None:
        try:
            saved = await read_operation(
                self._client,
                operation_key(self._key_prefix, "meeting", owner_id, operation.operation_id),
                operation,
            )
            return None if saved is None else await self._operation_result(saved, owner_id)
        except (RedisError, UnicodeError):
            raise MeetingStorageError("Meeting operation read failed") from None

    async def _operation_result(self, payload: str, owner_id: str) -> Meeting:
        try:
            record = self._decode(payload, UUID(json.loads(payload)["id"]))
            if record.owner_id != owner_id:
                raise ValueError("Operation owner mismatch")
            actual = await self._client.get(self._key(record.id))
            score = await self._client.zscore(
                index_key(self._key_prefix, "meeting", owner_id), index_member(record.starts_at, record.id)
            )
            if actual != payload or score != 0:
                raise ValueError("Operation result is inconsistent with record/index")
            return record
        except (RedisError, UnicodeError, ValueError, TypeError, KeyError):
            raise MeetingStorageError("Meeting operation result is invalid") from None
