"""Checks of tool arguments proposed by the LLM, against what the user actually said.

Some mistakes cannot be caught by the MCP server, because 00:00 is a valid time.
GigaChat-2-Max turns "напомни завтра позвонить маме" into a reminder at 00:00 in
every eval run, and no prompt wording fixed it (docs/ML_EVAL.md). AGENTS §7: a
reminder or meeting without a time needs a question, not an invented midnight.

The check needs the user's utterance, so the agent runner calls it before the tool
(see `app/agent/tools.py`, role 1).
"""

import re
from datetime import date, datetime
from typing import Any

TIMED_TYPES = frozenset({"reminder", "meeting"})

# The user did name midnight, or a relative time that may land on it ("через 2 часа" at 22:00).
# «Через неделю», «через месяц» name only a date: 00:00 there is invented like any other.
_MIDNIGHT_OR_RELATIVE_RE = re.compile(
    r"полноч|00[:.]00|\b0[:.]00|\b24[:.]00|12 ночи|двенадцать ночи|ноль часов|в ноль\b"
    r"|через\s+(?:\S+\s+)?(?:минут|час|полчас|сут)",
    re.IGNORECASE,
)

INVENTED_MIDNIGHT_ERROR = (
    "Пользователь не назвал время: 00:00 не подставляй. Запись не создана. Спроси, во сколько поставить."
)


def _local_midnight(value: Any) -> bool:
    if not isinstance(value, str):
        return False
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return False  # malformed values are rejected by the MCP server itself
    return (moment.hour, moment.minute, moment.second) == (0, 0, 0)


def invented_midnight(tool_name: str, args: dict[str, Any], user_text: str) -> str | None:
    """Error text for the model if a reminder/meeting got 00:00 the user never said, else None.

    Without the utterance (empty `user_text`) nothing is checked: there is nothing to compare with.
    """
    if not user_text.strip():
        return None
    if tool_name != "create_event" or args.get("event_type") not in TIMED_TYPES:
        return None
    if not _local_midnight(args.get("datetime")):
        return None
    if _MIDNIGHT_OR_RELATIVE_RE.search(user_text):
        return None
    return INVENTED_MIDNIGHT_ERROR


# The user named an end or a duration: "с 15 до 17", "15-17", "по 17:00", "на два часа", "длится час".
_END_OR_DURATION_RE = re.compile(
    r"\bдо\b|\bпо\s+\d|\d\s*[-–—]\s*\d|\bна\s+(\d+|пол|полтора|час|два|три|четыре|пять|сорок|тридцать)"
    r"|длит|продлится|займ[её]т",
    re.IGNORECASE,
)

INVENTED_END_ERROR = (
    "Пользователь не называл конец или длительность встречи: end_time не передавай. Запись не создана. "
    "Повтори вызов без end_time."
)


def invented_end(tool_name: str, args: dict[str, Any], user_text: str) -> str | None:
    """Error text if the model added a meeting end the user never mentioned (AGENTS §7), else None."""
    if not user_text.strip() or tool_name != "create_event" or not args.get("end_time"):
        return None
    return None if _END_OR_DURATION_RE.search(user_text) else INVENTED_END_ERROR


MONTHS_RU = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)  # fmt: skip
_ISO_DAY_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
DATE_FIELDS = ("datetime", "date", "end_time", "start_time")

NONEXISTENT_DATE_ERROR = (
    "Такой даты нет в календаре: {day}. Запись не создана. Скажи пользователю, что такой даты нет, "
    "и спроси, какую дату он имел в виду."
)


def nonexistent_date(args: dict[str, Any]) -> str | None:
    """Error text if a date does not exist («31 ноября»), else None.

    The MCP server rejects it too, but as «not ISO 8601», and the model then tells the user
    about the format instead of the date (h_nov31 in evals).
    """
    for key in DATE_FIELDS:
        value = args.get(key)
        match = _ISO_DAY_RE.match(value) if isinstance(value, str) else None
        if match is None:
            continue
        year, month, day = map(int, match.groups())
        if not (1 <= month <= 12 and 1 <= day <= 31):
            continue  # not a calendar mistake but a malformed value: left to the server
        try:
            date(year, month, day)
        except ValueError:
            return NONEXISTENT_DATE_ERROR.format(day=f"{day} {MONTHS_RU[month - 1]}")
    return None


_TODAY_RE = re.compile(r"\bсегодня\b", re.IGNORECASE)

PAST_TODAY_ERROR = (
    "{time} сегодня уже прошло: сейчас {now}. Запись не создана. Не уточняй тип записи, "
    "а спроси пользователя: «{time} уже прошло, поставить на завтра?»"
)


def past_today(
    tool_name: str, args: dict[str, Any], user_text: str, reference_time: datetime | None
) -> str | None:
    """Error text if the user said «сегодня» and the moment is already behind the reference time.

    Agreed policy (docs/ML_HANDOFF.md, N6): «сегодня в 17:30» said at 22:00 is a question
    «17:30 уже прошло, поставить на завтра?», not a record in the past. Without «сегодня» the
    nearest such time is tomorrow, which the model resolves itself.
    """
    if reference_time is None or tool_name != "create_event" or not _TODAY_RE.search(user_text):
        return None
    value = args.get("datetime")
    try:
        moment = datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None
    if moment is None or moment.utcoffset() is None or moment >= reference_time:
        return None
    local_now = reference_time.astimezone(moment.tzinfo)
    return PAST_TODAY_ERROR.format(time=moment.strftime("%H:%M"), now=local_now.strftime("%H:%M"))


def check_args(
    tool_name: str, args: dict[str, Any], user_text: str, reference_time: datetime | None = None
) -> str | None:
    """All checks of LLM-proposed arguments against the utterance; the first error, if any.

    `reference_time` is when the request was first received; without it the past-time check is off.
    """
    return (
        nonexistent_date(args)
        or past_today(tool_name, args, user_text, reference_time)
        or invented_midnight(tool_name, args, user_text)
        or invented_end(tool_name, args, user_text)
    )


# GigaChat sometimes closes the arguments object inside the last string value and keeps writing:
# a second call ("2026-10-20T09:00:00+03:00}} assistant function callrecipient{", when there are
# several records in one phrase) or its answer ("…+05:00}}![]() Напомню сегодня в 18:00…").
# After a "not ISO 8601" error it repeats the same call until the step limit. The tail after
# "}}" is cut off, so the intended call runs; the MCP server validates the value as usual.
_LEAKED_TAIL_RE = re.compile(r"\}\}.*$", re.DOTALL)


def normalize_args(tool_name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Repair known GigaChat output glitches before the call; the MCP server still validates.

    With several records in one phrase GigaChat also adds `date` equal to the day of `datetime`,
    or puts the time of the next record into `end_time` of a reminder («в 9 позвонить маме и в 18
    забрать посылку») and repeats the call unchanged after the error (multi_two_reminders: 1/3 against
    the real server on 26.10). Neither carries information about this record, so both are dropped
    and the next record is created by its own call. A `date` on another day is left to the server.
    """
    fixed = {k: _LEAKED_TAIL_RE.sub("", v) if isinstance(v, str) else v for k, v in args.items()}
    if tool_name == "get_event_by_query":
        return _without_type_words(fixed)
    if tool_name != "create_event":
        return fixed
    moment, day = fixed.get("datetime"), fixed.get("date")
    if isinstance(moment, str) and isinstance(day, str) and moment.startswith(day + "T"):
        fixed.pop("date")
    if fixed.get("event_type") != "meeting":
        fixed.pop("end_time", None)
    return fixed


# Shown to the user under a list of tasks for a period, when the list is built by code, not by the model.
# Words naming the record type. The search is full-text and needs every word, but the type is not
# in the title: «напоминание интернет» finds nothing for «Оплатить интернет» (search_internet, 26.10).
_TYPE_WORD_RE = re.compile(
    r"\b(?:задач[аеиуой]?|задачк\w*|напоминани\w*|встреч[аеиуой]?|запис[ьи]|дел[оа]?)\b", re.IGNORECASE
)


def _without_type_words(args: dict[str, Any]) -> dict[str, Any]:
    query = args.get("query")
    if not isinstance(query, str):
        return args
    rest = " ".join(_TYPE_WORD_RE.sub(" ", query).split())
    # Only type words («встреча») stay as they are: the search then looks for exactly them.
    return {**args, "query": rest} if rest else args


UNDATED_TASKS_ANSWER = "Задачи без срока сюда не входят, их можно найти по слову."
UNDATED_TASKS_NOTE = (
    "Задачи без срока в выборку за период не входят. Скажи пользователю, что здесь только задачи со "
    "сроком, а задачи без срока можно найти по слову."
)


# The search is lexical (PostgreSQL FTS): «созвон» does not find «Звонок», «зубной» does not find
# «стоматолог». One retry with other words of the same meaning covers most of these misses without
# embeddings (user's choice 27.10). This is not semantic search and is not called so.
# The search needs every word of the query (websearch_to_tsquery), so the retry joins the words and their
# synonyms with «or»: «созвон бухгалтер» repeated as «бухгалтер созвон» still missed «Звонок бухгалтеру».
SEARCH_RETRY_HINT = (
    "По этим словам ничего не найдено: поиск требует совпадения всех слов. Если ещё не повторял поиск, "
    "повтори get_event_by_query один раз: в query перечисли через or ключевые слова и их синонимы, "
    "каждый вариант — одно слово, например «созвон or звонок or бухгалтер», без слов «задача», «встреча», "
    "«напоминание». "
    "Если и так пусто — скажи пользователю, что не нашлось, и ничего не придумывай."
)


def annotate_result(tool_name: str, args: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """Add what the model must do or say but cannot see in the result.

    - The server selects tasks by deadline, so a list of tasks for a period never has the ones without
      one (INTEGRATION.md §3). A prompt rule alone did not make GigaChat say so (T3: 0/6 in evals, 6/6
      with this note). Only for task-only requests, not for «что у меня сегодня».
    - An empty search gets a hint to retry once with synonyms (`search_hint`: a key apart from `note`,
      which role 1's list output turns into the sentence about tasks without a deadline).
    """
    tasks_only = args.get("event_types") == ["task"]
    if tool_name == "get_events_by_period" and tasks_only and "events" in result:
        return {**result, "note": UNDATED_TASKS_NOTE}
    if tool_name == "get_event_by_query" and result.get("events") == []:
        return {**result, "search_hint": SEARCH_RETRY_HINT}
    return result
