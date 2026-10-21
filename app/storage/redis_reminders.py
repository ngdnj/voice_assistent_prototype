"""Versioned JSON reminder data, without delivery state or expiration."""

import json
from datetime import datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.planner.reminder import Reminder
from app.planner.reminder_repository import ReminderAlreadyExistsError, ReminderStorageError
from app.planner.query import QueryTooBroadError, RecordQuery
from app.storage.redis_index import (
    ADD_INDEXED, INDEX_EXISTING, index_key, index_member, read_index,
)



class RedisReminderRepository:
    def __init__(self, client: Redis, *, key_prefix: str = "it-gorky") -> None:
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be blank")
        self._client = client
        self._key_prefix = key_prefix

    def _key(self, reminder_id: UUID) -> str:
        return f"{self._key_prefix}:v1:reminder:{reminder_id}"

    async def add(self, reminder: Reminder) -> None:
        payload = json.dumps({
            "id": str(reminder.id), "owner_id": reminder.owner_id, "text": reminder.text,
            "remind_at": reminder.remind_at.isoformat(),
            "created_at": reminder.created_at.isoformat(),
        }, ensure_ascii=False)
        try:
            added = await self._client.eval(
                ADD_INDEXED, 2, self._key(reminder.id),
                index_key(self._key_prefix, "reminder", reminder.owner_id),
                payload, index_member(reminder.remind_at, reminder.id),
            )
        except RedisError:
            raise ReminderStorageError("Reminder storage write failed") from None
        if not added:
            raise ReminderAlreadyExistsError("Reminder ID already exists")

    async def get(self, *, reminder_id: UUID, owner_id: str) -> Reminder | None:
        try:
            payload = await self._client.get(self._key(reminder_id))
        except (RedisError, UnicodeError):
            raise ReminderStorageError("Reminder storage read failed") from None
        if payload is None:
            return None
        reminder = self._decode(payload, reminder_id)
        return reminder if reminder.owner_id == owner_id else None

    @staticmethod
    def _decode(payload: str | bytes, reminder_id: UUID) -> Reminder:
        try:
            data = json.loads(payload)
            if not isinstance(data, dict) or set(data) != {
                "id", "owner_id", "text", "remind_at", "created_at"
            }:
                raise ValueError("Invalid fields")
            if not all(isinstance(value, str) for value in data.values()):
                raise ValueError("Invalid field types")
            stored_id = UUID(data["id"])
            if stored_id != reminder_id:
                raise ValueError("Invalid identity")
            moments = {
                name: datetime.fromisoformat(data[name])
                for name in ("remind_at", "created_at")
            }
            if any(value.utcoffset() != timedelta(0) for value in moments.values()):
                raise ValueError("Expected UTC timestamps")
            reminder = Reminder(
                id=stored_id, owner_id=data["owner_id"], text=data["text"], **moments
            )
            if reminder.text != data["text"]:
                raise ValueError("Expected normalized text")
            return reminder
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ReminderStorageError("Stored reminder data is invalid") from None

    async def list(self, query: RecordQuery) -> list[Reminder]:
        try:
            members = await read_index(
                self._client, index_key(self._key_prefix, "reminder", query.owner_id), query
            )
            if not members:
                return []
            ids = [UUID(member.rsplit("|", 1)[1]) for member in members]
            payloads = await self._client.mget([self._key(record_id) for record_id in ids])
            records: list[Reminder] = []
            for record_id, member, payload in zip(ids, members, payloads, strict=True):
                if payload is None:
                    raise ValueError("Index points to a missing record")
                record = self._decode(payload, record_id)
                if (record.owner_id != query.owner_id
                        or index_member(record.remind_at, record.id) != member
                        or not query.includes(record.remind_at)):
                    raise ValueError("Index does not match stored record")
                if query.query is None or query.query in record.text.casefold():
                    records.append(record)
            if query.query is not None:
                return records[query.offset:query.offset + query.limit]
            return records
        except QueryTooBroadError:
            raise
        except (RedisError, UnicodeError, ValueError, IndexError):
            raise ReminderStorageError("Reminder index read failed") from None

    async def reindex(self) -> int:
        """Explicit offline backfill for legacy records; never called by list/get."""
        count = 0
        try:
            async for key in self._client.scan_iter(match=f"{self._key_prefix}:v1:reminder:*", count=100):
                record_id = UUID(key.rsplit(":", 1)[1])
                payload = await self._client.get(key)
                if payload is None:
                    continue
                record = self._decode(payload, record_id)
                await self._client.eval(
                    INDEX_EXISTING, 2, key,
                    index_key(self._key_prefix, "reminder", record.owner_id),
                    payload, index_member(record.remind_at, record.id),
                )
                count += 1
            return count
        except (RedisError, UnicodeError, ValueError):
            raise ReminderStorageError("Reminder index backfill failed") from None
