"""Redis wire contract for the MAX component, without a consumer or MAX client."""

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from redis.asyncio import Redis
from redis.exceptions import ResponseError
from redis.typing import EncodableT, FieldT

from app.planner.time import to_utc
from app.workers.notification import Notification

MAX_DELIVERIES = 3
RECLAIM_IDLE_MS = 60000
MAX_SEND_SECONDS = 20
ResultStatus = Literal["delivered", "retrying", "dead_lettered"]


class StaleDeliveryError(RuntimeError):
    """The pending entry now belongs to another consumer or delivery attempt."""


REPORT_RESULT = """local p = redis.call('XPENDING', KEYS[1], ARGV[1], ARGV[2], ARGV[2], 1)
if #p == 0 then return 0 end
if p[1][2] ~= ARGV[3] or p[1][4] ~= tonumber(ARGV[4]) then return -1 end
for i = 2, 3 do
    local kind = redis.call('TYPE', KEYS[i]).ok
    if kind ~= 'none' and kind ~= 'stream' then return redis.error_reply('Invalid result stream') end
end
redis.call('XADD', KEYS[2], '*', 'version', '1', 'data', ARGV[6])
if ARGV[5] == 'dead_lettered' then
    redis.call('XADD', KEYS[3], '*', 'version', '1', 'data', ARGV[7])
end
if ARGV[5] ~= 'retrying' then redis.call('XACK', KEYS[1], ARGV[1], ARGV[2]) end
return 1"""


@dataclass(frozen=True)
class NotificationStreams:
    prefix: str = "it-gorky"
    group: str = "max-delivery-v1"

    def __post_init__(self) -> None:
        if not self.prefix.strip() or not self.group.strip():
            raise ValueError("Stream prefix and consumer group must not be blank")

    @property
    def incoming(self) -> str:
        return f"{self.prefix}:v1:notifications:max"

    @property
    def results(self) -> str:
        return f"{self.prefix}:v1:notifications:results"

    @property
    def dead(self) -> str:
        return f"{self.prefix}:v1:notifications:dead"


class NotificationStream:
    def __init__(self, client: Redis, *, keys: NotificationStreams | None = None) -> None:
        self.client = client
        self.keys = NotificationStreams() if keys is None else keys

    async def publish(self, notification: Notification) -> str:
        fields: dict[FieldT, EncodableT] = {key: value for key, value in notification.to_fields().items()}
        result = await self.client.xadd(self.keys.incoming, fields)
        if not isinstance(result, str):
            raise ResponseError("Notification client must use decode_responses=True")
        return result

    async def ensure_group(self) -> None:
        try:
            await self.client.xgroup_create(self.keys.incoming, self.keys.group, id="0-0", mkstream=True)
        except ResponseError as error:
            if not str(error).startswith("BUSYGROUP"):
                raise

    async def report(
        self,
        *,
        entry_id: str,
        consumer: str,
        delivery_count: int,
        status: ResultStatus,
        fields: dict[str, str],
        recorded_at: datetime,
        outcome: str,
        error_code: str = "",
        external_message_id: str = "",
    ) -> bool:
        """Record an outcome and terminal ACK together; never send externally."""
        if status not in ("delivered", "retrying", "dead_lettered"):
            raise ValueError("Invalid result status")
        if outcome not in ("delivered", "retryable_failure", "permanent_failure", "unknown", "not_attempted"):
            raise ValueError("Invalid delivery outcome")
        if (status == "delivered") != (outcome == "delivered"):
            raise ValueError("Delivered status requires a confirmed successful outcome")
        if status == "retrying" and outcome not in ("retryable_failure", "unknown"):
            raise ValueError("Only temporary or unknown outcomes can be retried")
        if not consumer.strip() or delivery_count < 1:
            raise ValueError("Invalid consumer or delivery count")
        if status == "retrying" and delivery_count >= MAX_DELIVERIES:
            raise ValueError("Retry budget exhausted; dead-letter the notification")
        if error_code and (
            len(error_code) > 64 or not all(c in "abcdefghijklmnopqrstuvwxyz0123456789_" for c in error_code)
        ):
            raise ValueError("error_code must be a safe short code, not an exception message")
        try:
            notification = Notification.from_fields(fields)
        except (ValueError, TypeError):
            if status != "dead_lettered":
                raise ValueError("Invalid envelopes can only be dead-lettered") from None
            notification = None
        result = {
            "notification_id": None if notification is None else str(notification.notification_id),
            "owner_id": None if notification is None else notification.owner_id,
            "source_id": entry_id,
            "consumer": consumer,
            "delivery_count": delivery_count,
            "status": status,
            "outcome": outcome,
            "error_code": error_code,
            "external_message_id": external_message_id,
            "recorded_at": to_utc(recorded_at).isoformat(),
        }
        dead = {**result, "envelope": fields}
        code = await self.client.eval(
            REPORT_RESULT,
            3,
            self.keys.incoming,
            self.keys.results,
            self.keys.dead,
            self.keys.group,
            entry_id,
            consumer,
            delivery_count,
            status,
            json.dumps(result, ensure_ascii=False),
            json.dumps(dead, ensure_ascii=False),
        )
        if not isinstance(code, int) or code not in (-1, 0, 1):
            raise ResponseError("Invalid notification result response")
        if code == -1:
            raise StaleDeliveryError("Entry was claimed by another consumer or attempt")
        return code == 1
