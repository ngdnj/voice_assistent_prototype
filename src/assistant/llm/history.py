"""Dialog context per user, needed for clarifying questions and follow-ups.

A turn is the list of messages produced by one user request: the human message,
tool calls, tool results and the final answer. Turns are trimmed as a whole so
the history never starts with an orphan tool result.
"""

import json
from collections import defaultdict
from typing import Protocol

from langchain_core.messages import BaseMessage, messages_from_dict, messages_to_dict
from redis.asyncio import Redis


class ChatHistory(Protocol):
    async def load(self, user_id: str) -> list[BaseMessage]: ...

    async def append_turn(self, user_id: str, turn: list[BaseMessage]) -> None: ...

    async def clear(self, user_id: str) -> None: ...


class InMemoryChatHistory:
    def __init__(self, max_turns: int) -> None:
        self._max_turns = max_turns
        self._turns: dict[str, list[list[BaseMessage]]] = defaultdict(list)

    async def load(self, user_id: str) -> list[BaseMessage]:
        return [m for turn in self._turns[user_id] for m in turn]

    async def append_turn(self, user_id: str, turn: list[BaseMessage]) -> None:
        turns = self._turns[user_id]
        turns.append(turn)
        del turns[: -self._max_turns]

    async def clear(self, user_id: str) -> None:
        self._turns.pop(user_id, None)


class RedisChatHistory:
    def __init__(self, redis: Redis, max_turns: int, ttl_s: int) -> None:
        self._redis = redis
        self._max_turns = max_turns
        self._ttl_s = ttl_s

    @staticmethod
    def _key(user_id: str) -> str:
        return f"assistant:history:{user_id}"

    async def load(self, user_id: str) -> list[BaseMessage]:
        raw_turns = await self._redis.lrange(self._key(user_id), 0, -1)
        return [m for raw in raw_turns for m in messages_from_dict(json.loads(raw))]

    async def append_turn(self, user_id: str, turn: list[BaseMessage]) -> None:
        key = self._key(user_id)
        payload = json.dumps(messages_to_dict(turn), ensure_ascii=False)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, payload)
            pipe.ltrim(key, -self._max_turns, -1)
            pipe.expire(key, self._ttl_s)
            await pipe.execute()

    async def clear(self, user_id: str) -> None:
        await self._redis.delete(self._key(user_id))
