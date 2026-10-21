"""Catch answers that claim a record was saved or deleted when it was not.

LLMs sometimes reply «Встреча записана» without calling create_event, or
«Встреча удалена» without delete_event. For a personal planner this is the worst
failure: the user trusts a state that does not exist. The check is deterministic
and runs on every turn.
"""

import re
from enum import StrEnum

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage

CREATE_TOOL = "create_event"
READ_TOOLS = frozenset({"get_event", "get_event_by_query"})

# Past-tense verbs and short participles that mean "saved": записал, добавила, создана, сохранено...
# "запланировано" is left out on purpose: it is normal in answers about existing plans.
_CLAIM_RE = re.compile(
    r"\b(?:"
    r"(?:созда|добави|записа|сохрани|постави|назначи|установи)ла?"
    r"|(?:создан|добавлен|записан|сохранен|сохранён|поставлен|назначен|установлен)[аоы]?"
    r")\b",
    re.IGNORECASE,
)

RETRY_INSTRUCTION = (
    "[Проверка системы] В предыдущем ответе ты сообщил, что запись сохранена, но create_event "
    "не был успешно вызван — запись НЕ сохранена. Если данных достаточно, вызови create_event сейчас. "
    "Если данных не хватает, задай один уточняющий вопрос и не говори, что запись сохранена."
)

NOT_SAVED_ANSWER = "Не получилось сохранить запись. Повторите, пожалуйста, что и на когда записать."


DELETE_TOOL = "delete_event"

# "deleted": удалил, удалена, отменил, убрала...
_DELETE_CLAIM_RE = re.compile(
    r"\b(?:(?:удали|отмени|убра)ла?|(?:удален|удалён|отменен|отменён|убран)[аоы]?)\b",
    re.IGNORECASE,
)

DELETE_RETRY_INSTRUCTION = (
    "[Проверка системы] В предыдущем ответе ты сообщил, что запись удалена, но delete_event "
    "не был успешно вызван — запись НЕ удалена. Найди запись через get_event_by_query и вызови "
    "delete_event с её id. Если подходят несколько записей или ни одной — скажи об этом и не говори, "
    "что запись удалена."
)

NOT_DELETED_ANSWER = "Не получилось удалить запись. Уточните, пожалуйста, какую запись удалить."


class Claim(StrEnum):
    SAVED = "false_save_claim"
    DELETED = "false_delete_claim"


def claims_saved(text: str) -> bool:
    return bool(_CLAIM_RE.search(text))


def claims_deleted(text: str) -> bool:
    return bool(_DELETE_CLAIM_RE.search(text))


def _deleted(messages: list[BaseMessage]) -> bool:
    return any(
        isinstance(m, ToolMessage) and m.name == DELETE_TOOL and '"deleted"' in m.text for m in messages
    )


def is_false_delete_claim(turn: list[BaseMessage]) -> bool:
    final = turn[-1] if turn else None
    if not isinstance(final, AIMessage) or not claims_deleted(final.text):
        return False
    return not _deleted(turn)


def false_claim(turn: list[BaseMessage]) -> Claim | None:
    """Which action the final answer reports without having performed it, if any."""
    if is_false_delete_claim(turn):
        return Claim.DELETED
    if is_false_save_claim(turn):
        return Claim.SAVED
    return None


def retry_for(claim: Claim) -> HumanMessage:
    return HumanMessage(DELETE_RETRY_INSTRUCTION if claim is Claim.DELETED else RETRY_INSTRUCTION)


def fallback_for(claim: Claim) -> str:
    return NOT_DELETED_ANSWER if claim is Claim.DELETED else NOT_SAVED_ANSWER


def _created(messages: list[BaseMessage]) -> bool:
    """create_event succeeded, or found the same record already saved."""
    return any(
        isinstance(m, ToolMessage)
        and m.name == CREATE_TOOL
        and ('"created"' in m.text or '"already_exists"' in m.text)
        for m in messages
    )


def _used_read_tools(messages: list[BaseMessage]) -> bool:
    return any(
        isinstance(m, AIMessage) and any(c["name"] in READ_TOOLS for c in m.tool_calls) for m in messages
    )


def is_false_save_claim(turn: list[BaseMessage]) -> bool:
    """The final answer says "saved", but nothing was created in this turn.

    Turns that read records are skipped: «встреча назначена на 16:00» is then a
    description of an existing record, not a claim about a new one.
    """
    final = turn[-1] if turn else None
    if not isinstance(final, AIMessage) or not claims_saved(final.text):
        return False
    return not _created(turn) and not _used_read_tools(turn)
