"""Conservative evidence that the user supplied a time; never invent a moment.

This does not parse dates or calculate timestamps. Unsupported colloquial time
phrases require clarification rather than an arbitrary model-selected hour.
"""

import re
from collections.abc import Sequence

from langchain_core.messages import AIMessage, BaseMessage, HumanMessage

CLOCK = re.compile(r"(?<!\d)(?:[01]?\d|2[0-3]):[0-5]\d(?!\d)")
HOUR_WORD = (
    r"(?:час(?:у|а)?|од(?:ин|ного|ному)|дв(?:а|ух|ум)|три|тр[её]х|тр[её]м|четыр(?:е|[её]х|[её]м)|"
    r"пят(?:ь|и)|шест(?:ь|и)|сем(?:ь|и)|восемь|восьми|девят(?:ь|и)|десят(?:ь|и)|"
    r"одиннадцат(?:ь|и)|двенадцат(?:ь|и)|тринадцат(?:ь|и)|четырнадцат(?:ь|и)|"
    r"пятнадцат(?:ь|и)|шестнадцат(?:ь|и)|семнадцат(?:ь|и)|восемнадцат(?:ь|и)|"
    r"девятнадцат(?:ь|и)|двадцат(?:ь|и)|полдень|полудню|полночь|полуночи)"
)
HOUR = re.compile(
    rf"\b(?:в|к|с|со|до)\s+(?:(?:[01]?\d|2[0-3])(?=\s|[.!?,]|$)|{HOUR_WORD}\b)",
    re.I,
)
ORDINAL = (
    r"(?:первого|второго|третьего|четв[её]ртого|пятого|шестого|седьмого|восьмого|"
    r"девятого|десятого|одиннадцатого|двенадцатого)"
)
FRACTION = re.compile(
    rf"\b(?:(?:в\s+)?(?:половин[ае]|четверть)\s+{ORDINAL}|пол{ORDINAL}|"
    rf"без\s+(?:четверти|\d+|пяти|десяти|пятнадцати|двадцати)\s+{HOUR_WORD})\b",
    re.I,
)
# A complete request can say "завтра встреча, 10 утра" without a preposition.
# SHORT_HOUR alone only covers a separate clarification reply such as "10 утра".
WITH_DAY_PART = re.compile(
    rf"\b(?:[01]?\d|2[0-3]|{HOUR_WORD})\s*(?:часа?\s+|часов\s+)?(?:утра|дня|вечера|ночи)\b",
    re.I,
)
RELATIVE = re.compile(
    r"\b(?:через|спустя)\s+(?:(?:\d+|один|одну|два|две|три|четыре|пять|десять|пятнадцать|"
    r"двадцать|тридцать|полтора|полторы)\s+)?(?:минут\w*|час\w*|полчаса|день|дня|дней|сут\w*|недел\w*)\b",
    re.I,
)
SHORT_HOUR = re.compile(r"(?:[01]?\d|2[0-3])(?:\s*(?:утра|дня|вечера|ночи))?[.!]?", re.I)
CONFIRM = re.compile(r"(?:да|верно|хорошо|подтверждаю|так и сделай)[.!]?", re.I)


def has_explicit_time(text: str, history: Sequence[BaseMessage] = ()) -> bool:
    if (
        CLOCK.search(text)
        or HOUR.search(text)
        or FRACTION.search(text)
        or WITH_DAY_PART.search(text)
        or RELATIVE.search(text)
        or SHORT_HOUR.fullmatch(text.strip())
    ):
        return True
    if not history or not isinstance(history[-1], AIMessage) or "?" not in history[-1].text:
        return False
    question = history[-1].text
    if CONFIRM.fullmatch(text.strip()) and has_explicit_time(question):
        return True
    # A reply giving a missing title can inherit the user's time from the
    # immediately preceding clarification, not from arbitrary old records.
    if re.search(r"создал|записал|сохран|готово", question, re.I):
        return False
    previous = next((m for m in reversed(history[:-1]) if isinstance(m, HumanMessage)), None)
    return previous is not None and has_explicit_time(previous.text)
