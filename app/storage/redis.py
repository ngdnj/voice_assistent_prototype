"""Explicit Redis lifecycle; no connections are created during import."""

import asyncio
import math
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from redis.asyncio import Redis
from redis.asyncio.retry import Retry
from redis.backoff import NoBackoff
from redis.exceptions import RedisError


@dataclass(frozen=True)
class RedisSettings:
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    username: str | None = None
    password: str | None = field(default=None, repr=False)
    connect_timeout: float = 2.0
    command_timeout: float = 2.0

    def __post_init__(self) -> None:
        if not self.host.strip():
            raise ValueError("REDIS_HOST must not be blank")
        if not 1 <= self.port <= 65535:
            raise ValueError("REDIS_PORT must be between 1 and 65535")
        if self.db < 0:
            raise ValueError("REDIS_DB must not be negative")
        for name, value in (
            ("REDIS_CONNECT_TIMEOUT", self.connect_timeout),
            ("REDIS_COMMAND_TIMEOUT", self.command_timeout),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")

    @classmethod
    def from_env(cls) -> "RedisSettings":
        return cls(
            host=os.environ.get("REDIS_HOST", "127.0.0.1"),
            port=int(os.environ.get("REDIS_PORT", "6379")),
            db=int(os.environ.get("REDIS_DB", "0")),
            username=os.environ.get("REDIS_USERNAME") or None,
            password=os.environ.get("REDIS_PASSWORD") or None,
            connect_timeout=float(os.environ.get("REDIS_CONNECT_TIMEOUT", "2")),
            command_timeout=float(os.environ.get("REDIS_COMMAND_TIMEOUT", "2")),
        )


async def check_redis(client: Redis) -> bool:
    """Ping Redis; connection and timeout errors propagate to the caller."""
    return await client.ping()


@asynccontextmanager
async def redis_connection(settings: RedisSettings) -> AsyncIterator[Redis]:
    """Own one client/pool for a process lifecycle and close on every exit."""
    client = Redis(
        host=settings.host,
        port=settings.port,
        db=settings.db,
        username=settings.username,
        password=settings.password,
        socket_connect_timeout=settings.connect_timeout,
        socket_timeout=settings.command_timeout,
        decode_responses=True,
        retry=Retry(NoBackoff(), 0),
    )
    try:
        await check_redis(client)
        yield client
    finally:
        await client.aclose()


async def main() -> None:
    """Run a standalone availability check without printing connection details."""
    try:
        async with redis_connection(RedisSettings.from_env()):
            print("Redis доступен")
    except (RedisError, ValueError):
        raise SystemExit("Redis недоступен или настройки некорректны") from None


if __name__ == "__main__":
    asyncio.run(main())
