"""Dialog context per user, needed for clarifying questions and follow-ups.

A turn is the list of messages produced by one user request: the human message,
tool calls, tool results and the final answer. Turns are stored and trimmed as a
whole, so a tool call is never separated from its result and the history never
starts with an orphan tool result.

Two limits (AGENTS §10):
- storage: the last `max_turns` turns, TTL `ttl_s` (7 days), refreshed on every saved turn;
  reading does not refresh it, so an abandoned dialog expires 7 days after its last request;
- prompt: `load` returns the newest turns that fit into `max_tokens`, oldest dropped first.
"""

import json
from collections import defaultdict
from collections.abc import Sequence
from typing import Protocol

from langchain_core.messages import AIMessage, BaseMessage, messages_from_dict, messages_to_dict
from redis.asyncio import Redis

# Russian text in GigaChat tokenizer is ~3-4 characters per token; 3 overestimates,
# so the real prompt stays within the budget. Exact counts need an API call per load.
CHARS_PER_TOKEN = 3
MESSAGE_OVERHEAD_TOKENS = 4


def estimate_tokens(messages: Sequence[BaseMessage]) -> int:
    total = 0
    for message in messages:
        chars = len(message.text)
        if isinstance(message, AIMessage) and message.tool_calls:
            chars += len(json.dumps([[c["name"], c["args"]] for c in message.tool_calls], ensure_ascii=False))
        total += chars // CHARS_PER_TOKEN + MESSAGE_OVERHEAD_TOKENS
    return total


def fit_to_budget(turns: Sequence[list[BaseMessage]], max_tokens: int) -> list[BaseMessage]:
    """The newest whole turns whose estimated size fits into `max_tokens`."""
    kept: list[list[BaseMessage]] = []
    used = 0
    for turn in reversed(turns):
        size = estimate_tokens(turn)
        if used + size > max_tokens:
            break
        kept.append(turn)
        used += size
    return [m for turn in reversed(kept) for m in turn]


class ChatHistory(Protocol):
    async def load(self, user_id: str) -> list[BaseMessage]:
        """Messages to put into the prompt: whole turns, oldest first, within the token budget."""
        ...

    async def append_turn(self, user_id: str, turn: list[BaseMessage]) -> None: ...

    async def clear(self, user_id: str) -> None: ...


class InMemoryChatHistory:
    """For tests and the CLI without Redis; no TTL."""

    def __init__(self, max_turns: int, max_tokens: int = 3000) -> None:
        self._max_turns = max_turns
        self._max_tokens = max_tokens
        self._turns: dict[str, list[list[BaseMessage]]] = defaultdict(list)

    async def load(self, user_id: str) -> list[BaseMessage]:
        return fit_to_budget(self._turns[user_id], self._max_tokens)

    async def append_turn(self, user_id: str, turn: list[BaseMessage]) -> None:
        turns = self._turns[user_id]
        turns.append(turn)
        del turns[: -self._max_turns]

    async def clear(self, user_id: str) -> None:
        self._turns.pop(user_id, None)


class RedisChatHistory:
    """Redis list `assistant:history:<user_id>`, one JSON-encoded turn per element."""

    def __init__(self, redis: Redis, max_turns: int, ttl_s: int, max_tokens: int = 3000) -> None:
        self._redis = redis
        self._max_turns = max_turns
        self._ttl_s = ttl_s
        self._max_tokens = max_tokens

    @staticmethod
    def key(user_id: str) -> str:
        return f"assistant:history:{user_id}"

    async def load(self, user_id: str) -> list[BaseMessage]:
        raw_turns = await self._redis.lrange(self.key(user_id), 0, -1)
        turns = [messages_from_dict(json.loads(raw)) for raw in raw_turns]
        return fit_to_budget(turns, self._max_tokens)

    async def append_turn(self, user_id: str, turn: list[BaseMessage]) -> None:
        key = self.key(user_id)
        payload = json.dumps(messages_to_dict(turn), ensure_ascii=False)
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.rpush(key, payload)
            pipe.ltrim(key, -self._max_turns, -1)
            pipe.expire(key, self._ttl_s)
            await pipe.execute()

    async def clear(self, user_id: str) -> None:
        await self._redis.delete(self.key(user_id))
