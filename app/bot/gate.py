"""Redis state of the bot: one active request per user and webhook event deduplication.

Keys (INTEGRATION.md §6), user ids UTF-8 hex encoded as in app/storage/redis_index.py:
- `<prefix>:v1:bot:busy:<owner hex>` — string `job_id`, TTL BOT_BUSY_TTL_S;
- `<prefix>:v1:bot:event:<event hex>` — string "1", TTL EVENT_TTL_S;
- `<prefix>:v1:bot:voice:<owner hex>` — JSON of the recognized voice awaiting "Да, верно"/"Нет",
  TTL VOICE_CONFIRM_TTL_S; only the latest one per user is kept.
The lock has an owner (`job_id`) and a TTL, so a crashed process cannot block a user forever.
"""

from typing import Protocol

from redis.asyncio import Redis

# MAX retries a webhook for up to 8 hours; keep event ids a little longer.
EVENT_TTL_S = 9 * 3600
# How long a recognized voice message waits for the user's confirmation.
VOICE_CONFIRM_TTL_S = 15 * 60

# Delete only our own lock: a late finisher must not release a newer request's lock.
RELEASE = """if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0"""

# Take the pending voice only if it is the one the button refers to (older buttons are stale).
TAKE_VOICE = """local value = redis.call('GET', KEYS[1])
if value and cjson.decode(value)['id'] == ARGV[1] then
    redis.call('DEL', KEYS[1])
    return value
end
return false"""


class Gate(Protocol):
    async def first_seen(self, event_id: str) -> bool: ...
    async def forget(self, event_id: str) -> None: ...
    async def is_busy(self, owner_id: str) -> bool: ...
    async def acquire(self, owner_id: str, job_id: str) -> bool: ...
    async def release(self, owner_id: str, job_id: str) -> bool: ...
    async def save_voice(self, owner_id: str, pending: str) -> None: ...
    async def take_voice(self, owner_id: str, pending_id: str) -> str | None: ...


def _hex(value: str) -> str:
    return value.encode("utf-8").hex()


class RedisGate:
    def __init__(self, client: Redis, *, prefix: str = "it-gorky", busy_ttl_s: int = 180) -> None:
        if busy_ttl_s <= 0:
            raise ValueError("busy_ttl_s must be positive")
        self._client = client
        self._prefix = prefix
        self._busy_ttl_s = busy_ttl_s

    def busy_key(self, owner_id: str) -> str:
        return f"{self._prefix}:v1:bot:busy:{_hex(owner_id)}"

    def event_key(self, event_id: str) -> str:
        return f"{self._prefix}:v1:bot:event:{_hex(event_id)}"

    async def first_seen(self, event_id: str) -> bool:
        """True only for the first delivery of an event id (SET NX)."""
        return bool(await self._client.set(self.event_key(event_id), "1", nx=True, ex=EVENT_TTL_S))

    async def forget(self, event_id: str) -> None:
        """Allow MAX to redeliver an event whose admission failed."""
        await self._client.delete(self.event_key(event_id))

    async def is_busy(self, owner_id: str) -> bool:
        return bool(await self._client.exists(self.busy_key(owner_id)))

    async def acquire(self, owner_id: str, job_id: str) -> bool:
        return bool(await self._client.set(self.busy_key(owner_id), job_id, nx=True, ex=self._busy_ttl_s))

    async def release(self, owner_id: str, job_id: str) -> bool:
        return bool(await self._client.eval(RELEASE, 1, self.busy_key(owner_id), job_id))

    def voice_key(self, owner_id: str) -> str:
        return f"{self._prefix}:v1:bot:voice:{_hex(owner_id)}"

    async def save_voice(self, owner_id: str, pending: str) -> None:
        """Keep the recognized text (JSON with an `id`) until the user confirms or rejects it."""
        await self._client.set(self.voice_key(owner_id), pending, ex=VOICE_CONFIRM_TTL_S)

    async def take_voice(self, owner_id: str, pending_id: str) -> str | None:
        """Atomically remove and return the pending voice with this id; None if expired or replaced."""
        value = await self._client.eval(TAKE_VOICE, 1, self.voice_key(owner_id), pending_id)
        return value if isinstance(value, str) else None
