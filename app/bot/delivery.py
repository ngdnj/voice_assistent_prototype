"""Consumer of backend notifications (docs/notification-stream.md): Redis Stream -> MAX user.

One group `max-delivery-v1` for all bot instances, a unique consumer name per run. Results and ACK
go only through `NotificationStream.report` (role 1); this module never ACKs by itself.
The recipient is the trusted `owner_id`: for bot users it is their MAX user_id.
"""

import asyncio
import contextlib
import re
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol
from uuid import uuid4

from redis.exceptions import RedisError

from app.bot import metrics
from app.bot.client import SendResult
from app.bot.service import log_event
from app.storage.notification_stream import (
    MAX_DELIVERIES,
    MAX_SEND_SECONDS,
    RECLAIM_IDLE_MS,
    NotificationStream,
    ResultStatus,
    StaleDeliveryError,
)
from app.workers.notification import Notification

# BLOCK must stay below the Redis command timeout (2 s by default).
READ_BLOCK_MS = 1000
READ_COUNT = 10
RECLAIM_EVERY_S = 30.0
ERROR_PAUSE_S = 1.0
# MAX user ids are positive integers; anything else has no MAX recipient.
USER_ID = re.compile(r"[1-9][0-9]{0,19}")

Entry = tuple[str, dict[str, str]]


class UserSender(Protocol):
    async def send_to_user(self, *, user_id: str, text: str) -> SendResult: ...


class DeletedPayloadsError(RuntimeError):
    """XAUTOCLAIM reported pending entries whose payload is gone: stop and investigate."""


def consumer_name() -> str:
    return f"bot-{socket.gethostname()[:40]}-{uuid4().hex[:12]}"


async def pause(stop: asyncio.Event, seconds: float) -> None:
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), seconds)


def stream_entries(reply: Any, stream: str) -> list[Entry]:
    """Entries of one stream from XREADGROUP in RESP2 (list) or RESP3 (dict) form."""
    if not reply:
        return []
    if isinstance(reply, dict):
        batch = reply.get(stream)
        if batch is None:
            return []
        return list(batch[0] if batch and isinstance(batch[0], list) else batch)
    return list(reply[0][1])


class DeliveryConsumer:
    def __init__(
        self,
        stream: NotificationStream,
        sender: UserSender,
        *,
        name: str | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        reclaim_idle_ms: int = RECLAIM_IDLE_MS,
        block_ms: int = READ_BLOCK_MS,
        reclaim_every_s: float = RECLAIM_EVERY_S,
    ) -> None:
        self.stream = stream
        self.sender = sender
        self.name = consumer_name() if name is None else name
        self._clock = clock
        self._reclaim_idle_ms = reclaim_idle_ms
        self._block_ms = block_ms
        self._reclaim_every_s = reclaim_every_s
        self._cursor = "0-0"

    async def run(self, stop: asyncio.Event) -> None:
        """Read new entries and periodically reclaim stuck ones until `stop` is set."""
        loop = asyncio.get_running_loop()
        ready = False
        next_reclaim = 0.0
        while not stop.is_set():
            try:
                if not ready:
                    await self.stream.ensure_group()
                    ready = True
                    log_event("delivery_started")
                if loop.time() >= next_reclaim:
                    await self.reclaim()
                    next_reclaim = loop.time() + self._reclaim_every_s
                await self.read_new()
            except DeletedPayloadsError:
                # The contract asks for an alarm and a stop, not a silent skip.
                log_event("delivery_stopped", reason="deleted_pending_payloads")
                return
            except (RedisError, OSError):
                log_event("delivery_redis_error")
                await pause(stop, ERROR_PAUSE_S)

    async def read_new(self) -> int:
        reply = await self.stream.client.xreadgroup(
            self.stream.keys.group,
            self.name,
            {self.stream.keys.incoming: ">"},
            count=READ_COUNT,
            block=self._block_ms,
        )
        entries = stream_entries(reply, self.stream.keys.incoming)
        for entry in entries:
            await self.handle(entry)
        return len(entries)

    async def reclaim(self) -> int:
        """One XAUTOCLAIM batch from the saved cursor; `0-0` means the next pass starts over."""
        reply = await self.stream.client.xautoclaim(
            self.stream.keys.incoming,
            self.stream.keys.group,
            self.name,
            min_idle_time=self._reclaim_idle_ms,
            start_id=self._cursor,
            count=READ_COUNT,
        )
        cursor, entries = reply[0], reply[1]
        deleted = reply[2] if len(reply) > 2 else []
        if deleted or any(fields is None for _, fields in entries):
            raise DeletedPayloadsError("Pending payloads were deleted")
        self._cursor = cursor
        for entry in entries:
            await self.handle(entry)
        return len(entries)

    async def handle(self, entry: Entry) -> None:
        entry_id, fields = entry
        pending = await self.stream.client.xpending_range(
            self.stream.keys.incoming, self.stream.keys.group, entry_id, entry_id, 1
        )
        if not pending or pending[0]["consumer"] != self.name:
            log_event("delivery_stale")
            return
        count = int(pending[0]["times_delivered"])
        try:
            notification = Notification.from_fields(fields)
        except (ValueError, TypeError):
            await self._report(entry, count, "dead_lettered", "not_attempted", "invalid_envelope")
            return
        if count > MAX_DELIVERIES:
            # Earlier handlers died without a report: no fourth send.
            await self._report(entry, count, "dead_lettered", "not_attempted", "attempts_exhausted")
            return
        if not USER_ID.fullmatch(notification.owner_id):
            await self._report(entry, count, "dead_lettered", "permanent_failure", "unknown_recipient")
            return
        try:
            result = await asyncio.wait_for(
                self.sender.send_to_user(user_id=notification.owner_id, text=notification.text),
                timeout=MAX_SEND_SECONDS,
            )
        except TimeoutError:
            # The request may have reached MAX: unknown, not a failure.
            result = SendResult("unknown", error_code="send_timeout")
        if result.outcome == "delivered":
            await self._report(entry, count, "delivered", "delivered", "", result.external_message_id)
            return
        status: ResultStatus = (
            "dead_lettered"
            if count >= MAX_DELIVERIES or result.outcome == "permanent_failure"
            else "retrying"
        )
        await self._report(entry, count, status, result.outcome, result.error_code)

    async def _report(
        self,
        entry: Entry,
        count: int,
        status: ResultStatus,
        outcome: str,
        error_code: str,
        external_message_id: str = "",
    ) -> None:
        try:
            await self.stream.report(
                entry_id=entry[0],
                fields=entry[1],
                consumer=self.name,
                delivery_count=count,
                status=status,
                outcome=outcome,
                error_code=error_code,
                external_message_id=external_message_id,
                recorded_at=self._clock(),
            )
        except StaleDeliveryError:
            # Another consumer took the entry over: no further report from us.
            log_event("delivery_stale")
            return
        metrics.DELIVERIES.labels(status, outcome).inc()
        if status != "delivered":
            log_event("delivery_not_delivered", status=status, outcome=outcome, error_code=error_code)
