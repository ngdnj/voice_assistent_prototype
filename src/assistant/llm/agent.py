"""LLM agent: turns a user utterance into MCP tool calls and a spoken answer."""

import json
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from fastmcp import Client
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langgraph.errors import GraphRecursionError

from assistant import metrics
from assistant.llm import guard
from assistant.llm.history import ChatHistory
from assistant.llm.model import llm_session
from assistant.llm.prompts import render_system_prompt, user_timezone
from assistant.llm.speech import to_speech_text
from assistant.llm.tools import load_user_tools

FALLBACK_ANSWER = "Не получилось обработать запрос. Попробуйте сказать иначе."
STUCK_ANSWER = "Не получилось выполнить запрос. Уточните, пожалуйста, что и на когда записать."
# Graph steps per request: a model call and a tool call are one step each.
# Normal requests need at most 3 tool calls (7 steps).
RECURSION_LIMIT = 10


@dataclass(frozen=True, slots=True)
class ToolCall:
    """A tool call made in this turn and what the MCP server answered."""

    name: str
    args: dict[str, Any]  # as proposed by the LLM; user_id is added by the code, not shown here
    # Parsed JSON result, e.g. {"created": {...}} or {"error": "..."}; None if the tool did not answer.
    result: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and "error" not in self.result


@dataclass(frozen=True, slots=True)
class Pricing:
    input_rub_per_1k: float
    output_rub_per_1k: float

    def cost(self, input_tokens: int, output_tokens: int) -> float:
        return (input_tokens * self.input_rub_per_1k + output_tokens * self.output_rub_per_1k) / 1000


@dataclass(slots=True)
class AgentReply:
    """Result of one user message. `text` is ready to send: plain text, no markdown, one paragraph."""

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_rub: float = 0.0
    latency_s: float = 0.0
    retried: bool = False


class Assistant:
    def __init__(
        self,
        model: BaseChatModel,
        mcp_client: Client[Any],
        history: ChatHistory,
        pricing: Pricing,
    ) -> None:
        self._model = model
        self._mcp = mcp_client
        self._history = history
        self._pricing = pricing

    async def handle(self, user_id: str, text: str, *, reference_time: datetime, timezone: str) -> AgentReply:
        """Answer one user message.

        `reference_time` is when the message was first received (aware, any offset) and must stay
        the same when the request is retried; `timezone` is the IANA zone from the user profile.
        Relative dates ("завтра", "через 2 часа") are resolved from them, never from the clock.
        """
        started = time.perf_counter()
        system_prompt = render_system_prompt(reference_time, user_timezone(timezone))
        # Tools are bound to the user, so the agent graph is built per request (cheap).
        tools = await load_user_tools(self._mcp, user_id)
        agent = create_agent(self._model, tools)

        past = await self._history.load(user_id)
        user_message = HumanMessage(text)
        prefix: list[BaseMessage] = [SystemMessage(system_prompt), *past, user_message]
        with llm_session(user_id):
            first_try = await self._run(agent, prefix)

            turn = first_try
            spent = first_try
            claim = guard.false_claim(first_try)
            retried = claim is not None
            if claim is not None:
                # One retry with an explicit correction; the false answer is not kept in history.
                metrics.AGENT_RETRIES.labels(claim.value).inc()
                turn = await self._run(agent, [*prefix, *first_try, guard.retry_for(claim)])
                spent = [*first_try, *turn]

        reply = self._build_reply(turn, spent)
        reply.retried = retried
        still_false = guard.false_claim(turn)
        if still_false is not None:
            metrics.AGENT_ERRORS.labels(still_false.value).inc()
            reply.text = guard.fallback_for(still_false)
            turn = [*turn[:-1], AIMessage(reply.text)]
        await self._history.append_turn(user_id, [user_message, *turn])

        reply.latency_s = time.perf_counter() - started
        self._record_metrics(reply)
        return reply

    async def reset(self, user_id: str) -> None:
        await self._history.clear(user_id)

    @staticmethod
    async def _run(agent: Any, messages: list[BaseMessage]) -> list[BaseMessage]:
        """Run the agent graph and return only the new messages.

        A model that keeps calling a failing tool would loop until the graph limit;
        then the user gets a clarifying answer instead of a hang.
        """
        try:
            state = await agent.ainvoke({"messages": messages}, {"recursion_limit": RECURSION_LIMIT})
        except GraphRecursionError:
            metrics.AGENT_ERRORS.labels("tool_loop").inc()
            return [AIMessage(STUCK_ANSWER)]
        new_messages: list[BaseMessage] = state["messages"][len(messages) :]
        return new_messages

    def _build_reply(self, turn: list[BaseMessage], spent: list[BaseMessage]) -> AgentReply:
        """`turn` gives the answer and tool calls, `spent` all LLM calls paid for (incl. retries)."""
        reply = AgentReply(text=FALLBACK_ANSWER)
        results = {m.tool_call_id: _parse_result(m.text) for m in turn if isinstance(m, ToolMessage)}
        for message in turn:
            if isinstance(message, AIMessage):
                reply.tool_calls.extend(
                    ToolCall(c["name"], c["args"], results.get(c["id"] or "")) for c in message.tool_calls
                )
        for message in spent:
            if isinstance(message, AIMessage) and message.usage_metadata:
                reply.input_tokens += message.usage_metadata["input_tokens"]
                reply.output_tokens += message.usage_metadata["output_tokens"]
        final = turn[-1] if turn else None
        if isinstance(final, AIMessage) and final.text.strip():
            reply.text = to_speech_text(final.text)
        reply.cost_rub = self._pricing.cost(reply.input_tokens, reply.output_tokens)
        return reply

    @staticmethod
    def _record_metrics(reply: AgentReply) -> None:
        metrics.STAGE_LATENCY.labels("agent").observe(reply.latency_s)
        metrics.LLM_TOKENS.labels("input").inc(reply.input_tokens)
        metrics.LLM_TOKENS.labels("output").inc(reply.output_tokens)
        metrics.REQUEST_COST.observe(reply.cost_rub)
        for call in reply.tool_calls:
            metrics.TOOL_CALLS.labels(call.name).inc()


def _parse_result(text: str) -> dict[str, Any]:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return {"error": text}
    return parsed if isinstance(parsed, dict) else {"value": parsed}
