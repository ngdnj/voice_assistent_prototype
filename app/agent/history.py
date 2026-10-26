"""Whole dialog turns and resumable tool calls; no jobs or delivery queue."""

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from typing import Protocol
from uuid import uuid4

from langchain_core.messages import BaseMessage, messages_from_dict, messages_to_dict
from redis.asyncio import Redis


class ConversationBusyError(RuntimeError):
    pass


@dataclass
class TurnState:
    fingerprint: str
    messages: list[BaseMessage] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    retried: bool = False
    complete: bool = False
    has_explicit_time: bool = False

    def encode(self) -> str:
        return json.dumps(
            {
                "fingerprint": self.fingerprint,
                "messages": messages_to_dict(self.messages),
                "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens,
                "retried": self.retried,
                "complete": self.complete,
                "has_explicit_time": self.has_explicit_time,
            },
            ensure_ascii=False,
        )

    @classmethod
    def decode(cls, raw: str) -> "TurnState":
        value = json.loads(raw)
        return cls(
            fingerprint=value["fingerprint"],
            messages=messages_from_dict(value["messages"]),
            input_tokens=value["input_tokens"],
            output_tokens=value["output_tokens"],
            retried=value["retried"],
            complete=value["complete"],
            has_explicit_time=value.get("has_explicit_time", False),
        )


class ChatHistory(Protocol):
    def lock(self, owner: str, chat: str) -> AbstractAsyncContextManager[None]: ...
    async def load(self, owner: str, chat: str) -> list[BaseMessage]: ...
    async def get_turn(self, owner: str, chat: str, operation: str) -> TurnState | None: ...
    async def save_turn(self, owner: str, chat: str, operation: str, state: TurnState) -> None: ...
    async def reset(self, owner: str, chat: str) -> None: ...


class InMemoryChatHistory:
    """Demonstration only: all state is lost with this instance."""

    def __init__(self, max_turns: int = 20, max_tokens: int = 3000) -> None:
        self.max_turns, self.max_tokens = max_turns, max_tokens
        self._turns: dict[tuple[str, str, str], str] = {}
        self._completed: dict[tuple[str, str], list[str]] = {}
        self._busy: set[tuple[str, str]] = set()

    @asynccontextmanager
    async def lock(self, owner: str, chat: str) -> AsyncIterator[None]:
        key = (owner, chat)
        if key in self._busy:
            raise ConversationBusyError("Dialog is already processing")
        self._busy.add(key)
        try:
            yield
        finally:
            self._busy.remove(key)

    async def load(self, owner: str, chat: str) -> list[BaseMessage]:
        turns = [self._turns[(owner, chat, op)] for op in self._completed.get((owner, chat), [])]
        return _within_budget(turns[-self.max_turns :], self.max_tokens)

    async def get_turn(self, owner: str, chat: str, operation: str) -> TurnState | None:
        raw = self._turns.get((owner, chat, operation))
        return None if raw is None else TurnState.decode(raw)

    async def save_turn(self, owner: str, chat: str, operation: str, state: TurnState) -> None:
        self._turns[(owner, chat, operation)] = state.encode()
        if state.complete:
            completed = self._completed.setdefault((owner, chat), [])
            if operation not in completed:
                completed.append(operation)

    async def reset(self, owner: str, chat: str) -> None:
        self._completed.pop((owner, chat), None)


def _within_budget(raw_turns: list[str], max_tokens: int) -> list[BaseMessage]:
    chosen: list[list[BaseMessage]] = []
    size = 0
    for raw in reversed(raw_turns):
        messages = TurnState.decode(raw).messages
        cost = len(json.dumps(messages_to_dict(messages), ensure_ascii=False)) // 3 + 4 * len(messages)
        if size + cost > max_tokens:
            break
        size += cost
        chosen.append(messages)
    return [message for turn in reversed(chosen) for message in turn]


RELEASE = "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) end return 0"


class RedisChatHistory:
    def __init__(
        self, client: Redis, max_turns: int = 20, ttl_s: int = 604800, max_tokens: int = 3000
    ) -> None:
        if min(max_turns, ttl_s, max_tokens) <= 0:
            raise ValueError("History limits must be positive")
        self.client, self.max_turns, self.ttl_s, self.max_tokens = client, max_turns, ttl_s, max_tokens

    def _key(self, owner: str, chat: str) -> str:
        return f"it-gorky:v1:dialog:{owner.encode().hex()}:{chat.encode().hex()}"

    @asynccontextmanager
    async def lock(self, owner: str, chat: str) -> AsyncIterator[None]:
        key, token = self._key(owner, chat) + ":lock", str(uuid4())
        if not await self.client.set(key, token, nx=True, ex=90):
            raise ConversationBusyError("Dialog is already processing")
        try:
            yield
        finally:
            await asyncio.shield(self.client.eval(RELEASE, 1, key, token))

    async def load(self, owner: str, chat: str) -> list[BaseMessage]:
        values = await self.client.lrange(self._key(owner, chat) + ":history", 0, self.max_turns - 1)
        decoded: list[str] = []
        for value in reversed(values):
            if not isinstance(value, str):
                raise ValueError("History client requires decode_responses=True")
            decoded.append(value)
        return _within_budget(decoded, self.max_tokens)

    async def get_turn(self, owner: str, chat: str, operation: str) -> TurnState | None:
        raw = await self.client.get(self._key(owner, chat) + ":turn:" + operation.encode().hex())
        if raw is not None and not isinstance(raw, str):
            raise ValueError("History client requires decode_responses=True")
        return None if raw is None else TurnState.decode(raw)

    async def save_turn(self, owner: str, chat: str, operation: str, state: TurnState) -> None:
        base = self._key(owner, chat)
        key, history = base + ":turn:" + operation.encode().hex(), base + ":history"
        raw = state.encode()
        async with self.client.pipeline(transaction=True) as pipe:
            pipe.set(key, raw, ex=self.ttl_s)
            if state.complete:
                pipe.lpush(history, raw)
                pipe.ltrim(history, 0, self.max_turns - 1)
                pipe.expire(history, self.ttl_s)
            await pipe.execute()

    async def reset(self, owner: str, chat: str) -> None:
        await self.client.delete(self._key(owner, chat) + ":history")
