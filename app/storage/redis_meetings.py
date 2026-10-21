"""Versioned JSON storage for prototype meetings, without expiration."""

import json
from datetime import datetime, timedelta
from uuid import UUID

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.planner.meeting import Meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError, MeetingStorageError


class RedisMeetingRepository:
    def __init__(self, client: Redis, *, key_prefix: str = "it-gorky") -> None:
        if not key_prefix.strip():
            raise ValueError("key_prefix must not be blank")
        self._client = client
        self._key_prefix = key_prefix

    def _key(self, meeting_id: UUID) -> str:
        return f"{self._key_prefix}:v1:meeting:{meeting_id}"

    async def add(self, meeting: Meeting) -> None:
        payload = json.dumps({
            "id": str(meeting.id), "owner_id": meeting.owner_id, "title": meeting.title,
            "starts_at": meeting.starts_at.isoformat(),
            "ends_at": meeting.ends_at.isoformat(),
            "created_at": meeting.created_at.isoformat(),
        }, ensure_ascii=False)
        try:
            added = await self._client.set(self._key(meeting.id), payload, nx=True)
        except RedisError:
            raise MeetingStorageError("Meeting storage write failed") from None
        if not added:
            raise MeetingAlreadyExistsError("Meeting ID already exists")

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
                "id", "owner_id", "title", "starts_at", "ends_at", "created_at"
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
            }
            if any(value.utcoffset() != timedelta(0) for value in moments.values()):
                raise ValueError("Expected UTC timestamps")
            meeting = Meeting(
                id=stored_id, owner_id=data["owner_id"], title=data["title"], **moments
            )
            if meeting.title != data["title"]:
                raise ValueError("Expected normalized title")
            return meeting
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise MeetingStorageError("Stored meeting data is invalid") from None
