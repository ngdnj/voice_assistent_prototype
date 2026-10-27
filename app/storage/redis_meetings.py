"""Versioned JSON storage for prototype meetings, without expiration."""

import json
from datetime import datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.planner.meeting import Meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError, MeetingStorageError
from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.query import QueryTooBroadError, RecordQuery
from app.storage.redis_index import (
    ADD_INDEXED,
    INDEX_EXISTING,
    index_key,
    index_member,
    read_index,
)
from app.storage.redis_operation import commit_operation, operation_key, read_operation

# All checks precede writes: Redis Lua errors do not roll earlier writes back.
RESCHEDULE = """local saved = redis.call('GET', KEYS[3])
if saved then return {2, saved} end
local old = redis.call('GET', KEYS[1])
if not old then return {0, ''} end
if old ~= ARGV[1] then return {-1, ''} end
if redis.call('TYPE', KEYS[2]).ok ~= 'zset' then return redis.error_reply('Invalid index') end
if redis.call('ZSCORE', KEYS[2], ARGV[3]) ~= '0' then return redis.error_reply('Invalid member') end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('ZREM', KEYS[2], ARGV[3])
redis.call('ZADD', KEYS[2], 0, ARGV[4])
redis.call('SET', KEYS[3], ARGV[5] .. '\\n' .. ARGV[2])
return {1, ARGV[2]}"""


class RedisMeetingRepository:
    def __init__(self, client: Redis, *, key_prefix: str = "it-gorky") -> None:
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be blank")
        self._client = client
        self._key_prefix = key_prefix

    def _key(self, meeting_id: UUID) -> str:
        return f"{self._key_prefix}:v1:meeting:{meeting_id}"

    @staticmethod
    def _encode(meeting: Meeting) -> str:
        data = {
            "id": str(meeting.id),
            "owner_id": meeting.owner_id,
            "title": meeting.title,
            "description": meeting.description,
            "starts_at": meeting.starts_at.isoformat(),
            "created_at": meeting.created_at.isoformat(),
        }
        # A meeting without a known end stores no "ends_at".
        if meeting.ends_at is not None:
            data["ends_at"] = meeting.ends_at.isoformat()
        return json.dumps(data, ensure_ascii=False)

    async def add(self, meeting: Meeting, *, operation: CreationOperation | None = None) -> Meeting:
        payload = self._encode(meeting)
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
            # Records written before "description" existed have no such field; the end is optional.
            if not isinstance(data, dict) or set(data) - {"description", "ends_at"} != {
                "id",
                "owner_id",
                "title",
                "starts_at",
                "created_at",
            }:
                raise ValueError("Invalid fields")
            if not all(isinstance(value, str) for value in data.values()):
                raise ValueError("Invalid field types")
            stored_id = UUID(data["id"])
            if stored_id != meeting_id:
                raise ValueError("Invalid identity")
            moments = {
                name: datetime.fromisoformat(data[name])
                for name in ("starts_at", "ends_at", "created_at")
                if name in data
            }
            if any(value.utcoffset() != timedelta(0) for value in moments.values()):
                raise ValueError("Expected UTC timestamps")
            meeting = Meeting(
                id=stored_id,
                owner_id=data["owner_id"],
                title=data["title"],
                description=data.get("description", ""),
                **moments,
            )
            if meeting.title != data["title"] or meeting.description != data.get("description", ""):
                raise ValueError("Expected normalized title and description")
            return meeting
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MeetingStorageError("Stored meeting data is invalid") from None

    async def list(self, query: RecordQuery) -> list[Meeting]:
        if query.sort is not None:
            raise ValueError("Explicit sort requires the PostgreSQL adapter")
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
            if actual is None:
                raise ValueError("Operation result is missing")
            current = self._decode(actual, record.id)
            score = await self._client.zscore(
                index_key(self._key_prefix, "meeting", owner_id), index_member(current.starts_at, current.id)
            )
            if (current.owner_id, current.title, current.description, current.created_at) != (
                record.owner_id,
                record.title,
                record.description,
                record.created_at,
            ) or score != 0:
                raise ValueError("Operation result is inconsistent with record/index")
            return current
        except (RedisError, UnicodeError, ValueError, TypeError, KeyError):
            raise MeetingStorageError("Meeting operation result is invalid") from None

    def _reschedule_key(self, owner_id: str, operation: CreationOperation) -> str:
        # A separate operation namespace: legacy create keys stay compatible.
        return (
            f"{self._key_prefix}:v1:operation:reschedule_meeting:"
            f"{owner_id.encode().hex()}:{operation.operation_id.encode().hex()}"
        )

    async def get_rescheduled(self, *, owner_id: str, operation: CreationOperation) -> Meeting | None:
        try:
            saved = await read_operation(self._client, self._reschedule_key(owner_id, operation), operation)
            return None if saved is None else await self._operation_result(saved, owner_id)
        except (RedisError, UnicodeError):
            raise MeetingStorageError("Meeting operation read failed") from None

    async def reschedule(
        self,
        meeting: Meeting,
        *,
        previous: Meeting,
        operation: CreationOperation,
        changed_at: datetime,
    ) -> Meeting | None:
        try:
            replay = await self.get_rescheduled(owner_id=meeting.owner_id, operation=operation)
            if replay is not None:
                return replay
            raw = await self._client.get(self._key(meeting.id))
            if raw is None:
                return None
            current = self._decode(raw, meeting.id)
            if current.owner_id != meeting.owner_id:
                return None
            if (current.starts_at, current.ends_at) != (previous.starts_at, previous.ends_at):
                replay = await self.get_rescheduled(owner_id=meeting.owner_id, operation=operation)
                if replay is not None:
                    return replay
                raise OperationConflictError("Meeting changed concurrently; read it again")
            code, payload = await self._client.eval(
                RESCHEDULE,
                3,
                self._key(meeting.id),
                index_key(self._key_prefix, "meeting", meeting.owner_id),
                self._reschedule_key(meeting.owner_id, operation),
                raw,
                self._encode(meeting),
                index_member(previous.starts_at, previous.id),
                index_member(meeting.starts_at, meeting.id),
                operation.fingerprint,
            )
            if code == 0:
                return None
            if code == -1:
                # Another copy may have committed this very operation between our two reads.
                replay = await self.get_rescheduled(owner_id=meeting.owner_id, operation=operation)
                if replay is not None:
                    return replay
                raise OperationConflictError("Meeting changed concurrently; read it again")
            if code == 2:
                # Validate the saved fingerprint with the same parser as ordinary replays.
                replay = await self.get_rescheduled(owner_id=meeting.owner_id, operation=operation)
                if replay is None:
                    raise MeetingStorageError("Meeting operation is missing")
                return replay
            if code != 1 or not isinstance(payload, str):
                raise MeetingStorageError("Invalid reschedule result")
            return self._decode(payload, meeting.id)
        except (RedisError, UnicodeError):
            raise MeetingStorageError("Meeting reschedule failed") from None
