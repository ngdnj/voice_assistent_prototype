"""LLM agent: turns a user utterance into MCP tool calls and a spoken answer."""

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastmcp import Client
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage

from assistant import metrics
from assistant.llm.history import ChatHistory
from assistant.llm.prompts import render_system_prompt
from assistant.llm.tools import load_user_tools

FALLBACK_ANSWER = "Не получилось обработать запрос. Попробуйте сказать иначе."


@dataclass(frozen=True, slots=True)
class ToolCall:
    name: str
    args: dict[str, Any]


@dataclass(slots=True)
class AgentReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_rub: float = 0.0
    latency_s: float = 0.0


class Assistant:
    def __init__(
        self,
        model: BaseChatModel,
        mcp_client: Client[Any],
        history: ChatHistory,
        tz: ZoneInfo,
        price_rub_per_1k_tokens: float,
    ) -> None:
        self._model = model
        self._mcp = mcp_client
        self._history = history
        self._tz = tz
        self._price = price_rub_per_1k_tokens

    async def handle(self, user_id: str, text: str, now: datetime | None = None) -> AgentReply:
        started = time.perf_counter()
        now = now or datetime.now(self._tz)
        # Tools are bound to the user, so the agent graph is built per request (cheap).
        tools = await load_user_tools(self._mcp, user_id)
        agent = create_agent(self._model, tools)

        past = await self._history.load(user_id)
        user_message = HumanMessage(text)
        system = SystemMessage(render_system_prompt(now))
        state = await agent.ainvoke(cast(Any, {"messages": [system, *past, user_message]}))

        new_messages: list[BaseMessage] = state["messages"][len(past) + 2 :]
        reply = self._build_reply(new_messages)
        await self._history.append_turn(user_id, [user_message, *new_messages])

        reply.latency_s = time.perf_counter() - started
        self._record_metrics(reply)
        return reply

    async def reset(self, user_id: str) -> None:
        await self._history.clear(user_id)

    def _build_reply(self, messages: list[BaseMessage]) -> AgentReply:
        reply = AgentReply(text=FALLBACK_ANSWER)
        for message in messages:
            if not isinstance(message, AIMessage):
                continue
            reply.tool_calls.extend(ToolCall(c["name"], c["args"]) for c in message.tool_calls)
            if message.usage_metadata:
                reply.input_tokens += message.usage_metadata["input_tokens"]
                reply.output_tokens += message.usage_metadata["output_tokens"]
        final = messages[-1] if messages else None
        if isinstance(final, AIMessage) and final.text.strip():
            reply.text = final.text.strip()
        reply.cost_rub = (reply.input_tokens + reply.output_tokens) / 1000 * self._price
        return reply

    @staticmethod
    def _record_metrics(reply: AgentReply) -> None:
        metrics.STAGE_LATENCY.labels("agent").observe(reply.latency_s)
        metrics.LLM_TOKENS.labels("input").inc(reply.input_tokens)
        metrics.LLM_TOKENS.labels("output").inc(reply.output_tokens)
        metrics.REQUEST_COST.observe(reply.cost_rub)
        for call in reply.tool_calls:
            metrics.TOOL_CALLS.labels(call.name).inc()
