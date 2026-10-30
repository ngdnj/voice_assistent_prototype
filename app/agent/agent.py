"""Bounded text agent with a persisted tool plan and explicit trusted context."""

import asyncio
import hashlib
import json
import logging
import re
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid4, uuid5
from zoneinfo import ZoneInfo

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage

from app.agent.history import ChatHistory, TurnState
from app.agent.mcp import TOOLS, Caller, ToolGateway
from app.agent.permissions import (
    MUTATIONS,
    OFF_TOPIC_REPLY,
    allowed_mutations,
    confirmation_reply,
    off_topic,
    short_clarification,
)
from app.agent.presentation import format_event_list
from app.agent.time_input import has_explicit_time
from app.agent.tools import model_tool_schemas
from app.llm.arg_checks import annotate_result, check_args, normalize_args
from app.llm.model import llm_session
from app.llm.prompts import render_system_prompt, user_timezone
from app.llm.speech import to_chat_text
from app.mcp_server.schemas import EventDTO
from app.planner.operation import OperationConflictError
from app.planner.query import RECORD_SORTS, RecordSort
from app.planner.time import to_utc

LOGGER = logging.getLogger("it_gorky.agent")
MOVE_CLAIM = re.compile(
    r"\b(?:перен[её]с(?:ла|ли|[её]н[аоы]?)?|изменил[аи]?|измен[её]н[аоы]?|"
    r"обновил[аи]?|переименовал[аи]?|переименован[аоы]?|поменял[аи]?)\b",
    re.I,
)
DELETE_CLAIM = re.compile(
    r"\b(?:удалил[аи]?|удал[её]н[аоы]?|отменил[аи]?|отмен[её]н[аоы]?|убрал[аи]?)\b", re.I
)
MUTATING_TOOLS = frozenset({"create_event", "reschedule_meeting", "update_event", "delete_event"})
CLAIM = re.compile(
    r"\b(?:создал[аи]?|создан[аоы]?|добавил[аи]?|добавлен[аоы]?|записал[аи]?|записан[аоы]?|"
    r"поставил[аи]?|сохранил[аи]?|сохран[её]н[аоы]?|напомню|удалил[аи]?|готово|сделано)\b",
    re.I,
)
CONTEXT_FIELDS = frozenset({"owner_id", "user_id", "action_id", "operation_id", "request_id"})


@dataclass(frozen=True)
class Pricing:
    input_per_1k: float = 0.0
    output_per_1k: float = 0.0


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    result: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.result is not None and "error" not in self.result


@dataclass
class AgentReply:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cost_rub: float = 0.0
    latency_s: float = 0.0
    retried: bool = False


class TemporaryToolError(RuntimeError):
    pass


def _trace(messages: list[BaseMessage]) -> list[ToolCall]:
    results = {m.tool_call_id: json.loads(str(m.content)) for m in messages if isinstance(m, ToolMessage)}
    return [
        ToolCall(call["name"], call["args"], results.get(call.get("id") or ""))
        for message in messages
        if isinstance(message, AIMessage)
        for call in message.tool_calls
    ]


class Assistant:
    def __init__(
        self,
        model: BaseChatModel,
        mcp: ToolGateway,
        history: ChatHistory,
        pricing: Pricing | None = None,
        *,
        max_steps: int = 10,
        timeout_s: float = 60,
    ) -> None:
        if not 1 <= max_steps <= 20 or not 0 < timeout_s <= 60:
            raise ValueError("Agent limits must be positive and bounded")
        self.model, self.mcp, self.history, self.pricing = model, mcp, history, pricing or Pricing()
        self.max_steps, self.timeout_s = max_steps, timeout_s

    async def reset(self, user_id: str, *, chat_id: str = "private") -> None:
        async with self.history.lock(user_id, chat_id):
            await self.history.reset(user_id, chat_id)

    async def handle(
        self,
        user_id: str,
        text: str,
        *,
        reference_time: datetime,
        timezone: str,
        chat_id: str = "private",
        operation_id: str | None = None,
        request_id: str | None = None,
    ) -> AgentReply:
        """Caller authenticates the owner; operation_id identifies one turn, not one log request."""
        started = time.monotonic()
        operation_id = str(uuid4()) if operation_id is None else operation_id
        request_id = str(uuid4()) if request_id is None else request_id
        for value in (user_id, chat_id, operation_id, request_id):
            if not value.strip() or len(value) > 200 or any(ord(c) < 32 for c in value):
                raise ValueError("Invalid trusted context")
        if not text.strip() or len(text) > 12000:
            raise ValueError("Text must contain 1 to 12000 characters")
        moment, zone = to_utc(reference_time), user_timezone(timezone)
        fingerprint = hashlib.sha256(
            json.dumps([text, moment.isoformat(), timezone], ensure_ascii=False).encode()
        ).hexdigest()
        caller = Caller(user_id, timezone, request_id)
        async with self.history.lock(user_id, chat_id), asyncio.timeout(self.timeout_s):
            state = await self.history.get_turn(user_id, chat_id, operation_id)
            if state is None:
                context = await self.history.load(user_id, chat_id)
                state = TurnState(
                    fingerprint, [HumanMessage(text)], has_explicit_time=has_explicit_time(text, context)
                )
                latest = await self.history.latest(user_id, chat_id)
                pending = None if latest is None else latest.pending_change
                if off_topic(text):
                    state.messages.append(AIMessage(OFF_TOPIC_REPLY))
                    state.complete = True
                elif confirmation_reply(text):
                    if (
                        pending is not None
                        and 0
                        <= (moment - datetime.fromisoformat(pending["created_at"])).total_seconds()
                        <= 900
                    ):
                        state.approved_change = pending
                        state.messages.append(
                            AIMessage(
                                "",
                                tool_calls=[
                                    {
                                        "name": pending["name"],
                                        "args": pending["arguments"],
                                        "id": "approved-" + operation_id,
                                        "type": "tool_call",
                                    }
                                ],
                            )
                        )
                    else:
                        state.messages.append(
                            AIMessage("Нет действующего изменения для подтверждения. Повторите запрос.")
                        )
                        state.complete = True
                elif pending is not None and text.casefold().strip().rstrip(".! ") in {
                    "нет",
                    "отмена",
                    "не надо",
                }:
                    state.messages.append(AIMessage("Изменение отменено."))
                    state.complete = True
                elif latest is not None and latest.clarification_operation and short_clarification(text):
                    state.clarification_operation = latest.clarification_operation
                await self.history.save_turn(user_id, chat_id, operation_id, state)
            elif state.fingerprint != fingerprint:
                raise OperationConflictError("Turn was already used with different input or time context")
            if not state.complete and state.approved_change is not None:
                await self._pending(caller, chat_id, operation_id, state, moment)
                call = _trace(state.messages)[-1]
                if call.ok and call.result is not None:
                    record = call.result.get("deleted", call.result.get("updated", {}))
                    verb = "Удалено" if call.name == "delete_event" else "Изменено"
                    answer_text = f"{verb}: {record.get('name', 'запись')}."
                else:
                    answer_text = "Изменение не выполнено. Перечитайте запись и повторите запрос."
                state.messages.append(AIMessage(answer_text))
                state.complete = True
                await self.history.save_turn(user_id, chat_id, operation_id, state)
            if not state.complete:
                context = await self.history.load(user_id, chat_id)
                schemas = await self.mcp.list_tools(caller)
                bound = self.model.bind_tools(model_tool_schemas(schemas))
                system = SystemMessage(render_system_prompt(moment, zone))
                for _ in range(self.max_steps):
                    question = await self._pending(caller, chat_id, operation_id, state, moment)
                    if question is not None:
                        state.messages.append(AIMessage(question))
                        state.complete = True
                        break
                    steps = sum(isinstance(m, AIMessage) for m in state.messages)
                    if steps >= self.max_steps:
                        break
                    messages = [system, *context, *state.messages]
                    # Bound the entire current prompt as well as historical turns.
                    if (
                        len(json.dumps([m.model_dump(mode="json") for m in messages], ensure_ascii=False))
                        + len(json.dumps(schemas, ensure_ascii=False))
                        > 48000
                    ):
                        state.messages.append(
                            AIMessage("Слишком много данных для обработки. Уточните период или поиск.")
                        )
                        state.complete = True
                        break
                    if state.retried:
                        messages[0] = SystemMessage(
                            system.text + "\nНе подтверждай изменение без успешного результата инструмента. "
                            "Если данных не хватает, задай конкретный уточняющий вопрос."
                        )
                    with llm_session(user_id):
                        answer = await bound.ainvoke(messages)
                    if not isinstance(answer, AIMessage):
                        raise RuntimeError("Model returned an invalid message")
                    if answer.usage_metadata:
                        state.input_tokens += answer.usage_metadata["input_tokens"]
                        state.output_tokens += answer.usage_metadata["output_tokens"]
                    if answer.invalid_tool_calls:
                        raise ValueError("Model returned malformed tool arguments")
                    if answer.tool_calls:
                        previous_ids = {
                            c.get("id")
                            for m in state.messages
                            if isinstance(m, AIMessage)
                            for c in m.tool_calls
                        }
                        ids = [c.get("id") for c in answer.tool_calls]
                        total = sum(len(m.tool_calls) for m in state.messages if isinstance(m, AIMessage))
                        if (
                            len(ids) > 5
                            or total + len(ids) > 20
                            or None in ids
                            or len(set(ids)) != len(ids)
                            or previous_ids.intersection(ids)
                        ):
                            raise ValueError("Invalid or excessive tool call batch")
                        # Persist the model's exact plan before any side effect.
                        for proposed in answer.tool_calls:
                            proposed["args"] = normalize_args(proposed["name"], proposed["args"])
                        state.messages.append(answer)
                        await self.history.save_turn(user_id, chat_id, operation_id, state)
                        continue
                    plain = to_chat_text(answer.text)
                    if plain.rstrip().endswith("?") and "create_event" in allowed_mutations(text):
                        state.clarification_operation = "create_event"
                    listing = self._list_answer(state, plain, moment, zone)
                    if listing is not None:
                        state.messages.append(AIMessage(listing))
                        state.complete = True
                        break
                    saved = self._saved(state)
                    if (
                        (CLAIM.search(plain) and not saved)
                        or (
                            MOVE_CLAIM.search(plain)
                            and not (
                                self._saved(state, name="reschedule_meeting")
                                or self._saved(state, name="update_event")
                            )
                        )
                        or (DELETE_CLAIM.search(plain) and not self._saved(state, name="delete_event"))
                    ):
                        if not state.retried:
                            state.retried = True
                            await self.history.save_turn(user_id, chat_id, operation_id, state)
                            continue
                        plain = "Изменение не подтверждено. Уточните нужную запись."
                    state.messages.append(AIMessage(plain or "Уточните, пожалуйста, ваш запрос."))
                    state.complete = True
                    break
                if not state.complete:
                    # Resolve the final admitted tool calls before reporting the step limit.
                    question = await self._pending(caller, chat_id, operation_id, state, moment)
                    saved = self._saved(state)
                    state.messages.append(
                        AIMessage(
                            question
                            if question is not None
                            else "Действие выполнено, но не удалось сформировать полный ответ."
                            if saved
                            else "Не удалось завершить обработку за отведённое число шагов."
                        )
                    )
                    state.complete = True
                await self.history.save_turn(user_id, chat_id, operation_id, state)
            LOGGER.info(json.dumps({"event": "agent_completed", "request_id": request_id}))
            cost = (
                state.input_tokens * self.pricing.input_per_1k
                + state.output_tokens * self.pricing.output_per_1k
            ) / 1000
            return AgentReply(
                str(state.messages[-1].content),
                _trace(state.messages),
                state.input_tokens,
                state.output_tokens,
                cost,
                time.monotonic() - started,
                state.retried,
            )

    async def _pending(
        self,
        caller: Caller,
        chat: str,
        operation: str,
        state: TurnState,
        reference_time: datetime,
    ) -> str | None:
        completed = {m.tool_call_id for m in state.messages if isinstance(m, ToolMessage)}
        ordinal = 0
        question = None
        question_field = "datetime"
        for message in list(state.messages):
            if not isinstance(message, AIMessage):
                continue
            for call in message.tool_calls:
                ordinal += 1
                call_id = call.get("id")
                if not call_id:
                    raise ValueError("Model tool call must have an ID")
                if call_id in completed:
                    continue
                name = call["name"]
                arguments = normalize_args(name, call["args"])
                needs_time = (
                    name == "create_event"
                    and arguments.get("event_type") in ("reminder", "meeting")
                    and not state.has_explicit_time
                    and not has_explicit_time(state.messages[0].text)
                )
                approved = state.approved_change
                is_approved = (
                    approved is not None and approved["name"] == name and approved["arguments"] == arguments
                )
                action = (
                    approved["action_id"]
                    if is_approved and approved is not None
                    else str(uuid5(NAMESPACE_URL, json.dumps([caller.owner_id, chat, operation, ordinal])))
                )
                capabilities = allowed_mutations(state.messages[0].text)
                permitted = (
                    name not in MUTATIONS
                    or name in capabilities
                    or is_approved
                    or (name == state.clarification_operation and short_clarification(state.messages[0].text))
                )
                previous = next(
                    (
                        c.result
                        for c in _trace(state.messages)
                        if name in MUTATING_TOOLS and c.name == name and c.args == arguments and c.ok
                    ),
                    None,
                )
                if (
                    permitted
                    and not is_approved
                    and name in {"update_event", "delete_event", "reschedule_meeting"}
                    and previous is None
                ):
                    choices = self._ambiguous_choices(state, arguments)
                    if choices:
                        question_field = "meeting_id" if name == "reschedule_meeting" else "event_id"
                        question = format_event_list(
                            choices, reference_time=reference_time, timezone=user_timezone(caller.timezone)
                        ) + (
                            "\n\nКакую запись удалить?"
                            if name == "delete_event"
                            else "\n\nКакую запись изменить?"
                        )
                if not permitted:
                    result: dict[str, Any] = {
                        "error": {
                            "code": "operation_not_authorized",
                            "message": "Текущая реплика не разрешает это действие. Данные не изменены.",
                            "field": None,
                        }
                    }
                elif needs_time or question is not None:
                    state.clarification_operation = name
                    question = question or (
                        "Во сколько напомнить?"
                        if arguments.get("event_type") == "reminder"
                        else "Во сколько начнётся встреча?"
                    )
                    result = {
                        "error": {
                            "code": "validation_error",
                            "message": "Для выполнения действия нужно уточнение пользователя",
                            "field": question_field,
                        }
                    }
                elif name not in TOOLS or set(arguments) & CONTEXT_FIELDS:
                    result = {
                        "error": {
                            "code": "validation_error",
                            "message": "Недопустимый инструмент или контекст",
                            "field": None,
                        }
                    }
                elif previous is not None:
                    result = previous
                elif name in {"update_event", "delete_event", "reschedule_meeting"} and not is_approved:
                    state.pending_change = {
                        "name": name,
                        "arguments": arguments,
                        "action_id": action,
                        "created_at": reference_time.isoformat(),
                    }
                    label = {
                        "delete_event": "Удалить",
                        "update_event": "Изменить",
                        "reschedule_meeting": "Перенести",
                    }[name]
                    target_id = arguments.get("event_id", arguments.get("meeting_id"))
                    prior_messages = await self.history.load(caller.owner_id, chat)
                    records = [
                        event
                        for c in _trace([*prior_messages, *state.messages])
                        if c.ok and c.result
                        for event in (
                            c.result.get("events", [])
                            + [c.result[key] for key in ("created", "updated", "deleted") if key in c.result]
                        )
                        if event.get("id") == target_id
                    ]
                    title = " ".join(str(records[-1]["name"]).split()) if records else str(target_id)
                    fields = {k: v for k, v in arguments.items() if k not in {"event_id", "meeting_id"}}
                    question = f"{label} «{title}»?"
                    labels = {
                        "name": "Название",
                        "datetime": "Начало / срок",
                        "date": "Дата",
                        "end_time": "Окончание",
                    }
                    for key, value in fields.items():
                        display = str(value)
                        if key in {"datetime", "end_time"}:
                            with suppress(ValueError):
                                parsed = datetime.fromisoformat(display)
                                if parsed.tzinfo is None:
                                    raise ValueError("An explicit timezone is required")
                                display = parsed.astimezone(user_timezone(caller.timezone)).strftime(
                                    "%d.%m.%Y %H:%M"
                                )
                        question += f"\n{labels.get(key, key)}: {' '.join(display.split())}"
                    question += "\nОтветьте «да» для подтверждения или «нет» для отмены (15 минут)."
                    result = {"error": {"code": "confirmation_required", "message": question, "field": None}}
                else:
                    validation = (
                        check_args(
                            name,
                            arguments,
                            state.messages[0].text,
                            reference_time,
                            permitted_operations=frozenset({name}) if permitted else frozenset(),
                        )
                        if name == "create_event"
                        else None
                    )
                    if validation:
                        result = {"error": {"code": "validation_error", "message": validation, "field": None}}
                    else:
                        result = await self.mcp.call(caller, name, arguments, action_id=action)
                        result = annotate_result(name, arguments, result)
                    if result.get("error", {}).get("code") == "temporary_unavailable":
                        raise TemporaryToolError("Tool storage is temporarily unavailable; retry this turn")
                state.messages.append(
                    ToolMessage(
                        json.dumps(result, ensure_ascii=False),
                        tool_call_id=call_id,
                        status="error" if "error" in result else "success",
                    )
                )
                completed.add(call_id)
                await self.history.save_turn(caller.owner_id, chat, operation, state)
        return question

    @staticmethod
    def _ambiguous_choices(state: TurnState, arguments: dict[str, Any]) -> list[EventDTO]:
        read = next(
            (
                call
                for call in reversed(_trace(state.messages))
                if call.ok and call.name in {"get_events_by_period", "get_event_by_query"}
            ),
            None,
        )
        if read is None or read.result is None:
            return []
        choices = [EventDTO.model_validate(raw) for raw in read.result.get("events", [])]
        if len(choices) < 2:
            return []
        # A unique full title in the user's request is an explicit selection. Otherwise
        # do not let the model choose an arbitrary record from an ambiguous search.
        selected = [
            event
            for event in choices
            if re.search(
                r"(?<!\w)" + re.escape(event.name.casefold()) + r"(?!\w)", state.messages[0].text.casefold()
            )
        ]
        target = arguments.get("event_id", arguments.get("meeting_id"))
        if len(selected) == 1 and str(selected[0].id) == target:
            return []
        return choices

    @staticmethod
    def _list_answer(state: TurnState, plain: str, reference_time: datetime, zone: ZoneInfo) -> str | None:
        calls = _trace(state.messages)
        read_tools = {"get_events_by_period", "get_event_by_query"}
        # Searches used to choose a meeting for rescheduling must keep the model's question.
        if (
            not calls
            or re.search(
                r"^\s*(?:а\s+)?(?:(?:скажи|подскажи)\s*[, ]\s*)?"
                r"(?:сколько\b|когда\b|во сколько\b|где\b|почему\b|зачем\b|как(?:ой|ая|ое)?\b)",
                state.messages[0].text,
                re.I,
            )
            or (plain.rstrip().endswith("?") and allowed_mutations(state.messages[0].text))
            or any(call.name not in read_tools for call in calls)
        ):
            return None
        if not calls[-1].ok:
            return None
        records = {}
        truncated = False
        for call in calls:
            if not call.ok or call.result is None:
                continue
            for raw in call.result.get("events", []):
                event = EventDTO.model_validate(raw)
                records[event.id] = event
            truncated |= bool(call.result.get("truncated"))
        sort = calls[-1].args.get("sort") or "time_asc"
        if sort not in RECORD_SORTS:
            raise ValueError("Invalid result ordering")
        # A requested top N is intentionally short. The formatter still warns if
        # its own display/length budget cannot fit those N records.
        if calls[-1].args.get("limit") is not None:
            truncated = False
        return format_event_list(
            list(records.values()),
            reference_time=reference_time,
            timezone=zone,
            sort=cast(RecordSort, sort),
            truncated=truncated,
        )

    @staticmethod
    def _saved(state: TurnState, *, name: str | None = None) -> bool:
        return any(
            c.ok
            and c.result
            and (
                (c.name == "create_event" and "created" in c.result)
                or (c.name == "reschedule_meeting" and "updated" in c.result)
                or (c.name == "update_event" and "updated" in c.result)
                or (c.name == "delete_event" and "deleted" in c.result)
            )
            for c in _trace(state.messages)
            if name is None or c.name == name
        )
