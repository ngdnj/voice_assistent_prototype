"""Explicit offline migration: backfill owner/time indexes for existing JSON."""

import argparse
import asyncio

from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_meetings import RedisMeetingRepository
from app.storage.redis_reminders import RedisReminderRepository
from app.storage.redis_tasks import RedisTaskRepository


async def reindex(key_prefix: str) -> None:
    async with redis_connection(RedisSettings.from_env()) as client:
        for repository in (
            RedisTaskRepository(client, key_prefix=key_prefix),
            RedisMeetingRepository(client, key_prefix=key_prefix),
            RedisReminderRepository(client, key_prefix=key_prefix),
        ):
            count = await repository.reindex()
            print(f"{type(repository).__name__}: indexed {count} records")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--key-prefix", default="it-gorky")
    asyncio.run(reindex(parser.parse_args().key_prefix))
