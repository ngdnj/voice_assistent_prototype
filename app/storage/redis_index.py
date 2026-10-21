"""Small Redis indexing primitives shared by the three concrete repositories."""

from datetime import datetime
from uuid import UUID

from redis.asyncio import Redis

from app.planner.query import MAX_SEARCH_CANDIDATES, QueryTooBroadError, RecordQuery

# Validate key types before writes: Redis Lua scripts do not roll back runtime errors.
ADD_INDEXED = """
if redis.call('EXISTS', KEYS[1]) == 1 then return 0 end
local kind = redis.call('TYPE', KEYS[2]).ok
if kind ~= 'none' and kind ~= 'zset' then
    return redis.error_reply('Invalid index type')
end
redis.call('SET', KEYS[1], ARGV[1], 'NX')
redis.call('ZADD', KEYS[2], 0, ARGV[2])
return 1
"""

INDEX_EXISTING = """
if redis.call('GET', KEYS[1]) ~= ARGV[1] then
    return redis.error_reply('Record changed during indexing')
end
local kind = redis.call('TYPE', KEYS[2]).ok
if kind ~= 'none' and kind ~= 'zset' then
    return redis.error_reply('Invalid index type')
end
return redis.call('ZADD', KEYS[2], 0, ARGV[2])
"""


def index_key(prefix: str, kind: str, owner_id: str) -> str:
    return f"{prefix}:v1:index:{kind}:owner:{owner_id.encode('utf-8').hex()}"


def index_member(moment: datetime, record_id: UUID) -> str:
    return f"{moment.isoformat(timespec='microseconds')}|{record_id}"


async def read_index(client: Redis, key: str, query: RecordQuery) -> list[str]:
    lower = "-" if query.start_time is None else f"[{query.start_time.isoformat(timespec='microseconds')}|"
    upper = "+" if query.end_time is None else f"({query.end_time.isoformat(timespec='microseconds')}|"
    if query.query is None:
        members = await client.zrangebylex(key, lower, upper, start=query.offset, num=query.limit)
    else:
        members = await client.zrangebylex(key, lower, upper, start=0, num=MAX_SEARCH_CANDIDATES + 1)
        if len(members) > MAX_SEARCH_CANDIDATES:
            raise QueryTooBroadError("Search exceeds 1000 candidates; narrow the period")
    # redis-py types allow bytes even though the shared client decodes responses.
    # Reject a misconfigured client explicitly, rather than returning invalid data.
    decoded: list[str] = []
    for member in members:
        if not isinstance(member, str):
            raise ValueError("Index client must use decode_responses=True")
        decoded.append(member)
    return decoded
