"""One atomic Redis commit for a record, its index and its replay result."""

from redis.asyncio import Redis
from redis.exceptions import ResponseError

from app.planner.operation import CreationOperation, OperationConflictError

CREATE_ONCE = """local old = redis.call('GET', KEYS[3])
if old then
    if #old < 66 or string.sub(old, 65, 65) ~= '\\n' then
        return redis.error_reply('Invalid operation result')
    end
    if string.sub(old, 1, 64) ~= ARGV[1] then return {-1, ''} end
    return {1, string.sub(old, 66)}
end
if redis.call('EXISTS', KEYS[1]) == 1 then return {0, ''} end
local kind = redis.call('TYPE', KEYS[2]).ok
if kind ~= 'none' and kind ~= 'zset' then
    return redis.error_reply('Invalid index type')
end
redis.call('SET', KEYS[1], ARGV[2])
redis.call('ZADD', KEYS[2], 0, ARGV[3])
redis.call('SET', KEYS[3], ARGV[1] .. '\\n' .. ARGV[2])
return {1, ARGV[2]}"""


def operation_key(prefix: str, kind: str, owner_id: str, operation_id: str) -> str:
    return (
        f"{prefix}:v1:operation:create_{kind}:"
        f"{owner_id.encode('utf-8').hex()}:{operation_id.encode('utf-8').hex()}"
    )


async def read_operation(client: Redis, key: str, operation: CreationOperation) -> str | None:
    saved = await client.get(key)
    if saved is None:
        return None
    if not isinstance(saved, str) or len(saved) < 66 or saved[64] != "\n":
        raise ResponseError("Invalid operation result")
    if saved[:64] != operation.fingerprint:
        raise OperationConflictError("operation_id was already used with different data")
    return saved[65:]


async def commit_operation(
    client: Redis,
    *,
    record_key: str,
    index_key: str,
    operation_key: str,
    operation: CreationOperation,
    payload: str,
    member: str,
) -> str | None:
    reply = await client.eval(
        CREATE_ONCE,
        3,
        record_key,
        index_key,
        operation_key,
        operation.fingerprint,
        payload,
        member,
    )
    if not isinstance(reply, list) or len(reply) != 2:
        raise ResponseError("Invalid operation commit response")
    code, saved = reply
    if code not in (-1, 0, 1) or not isinstance(saved, str):
        raise ResponseError("Invalid operation commit response")
    if code == -1:
        raise OperationConflictError("operation_id was already used with different data")
    return None if code == 0 else saved
