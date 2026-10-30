"""Turn-scoped write permissions derived before reading model/tool output."""

import re

MUTATIONS = frozenset({"create_event", "update_event", "delete_event", "reschedule_meeting"})
CONFIRMATION_INSTRUCTION = "\nОтветьте «да» для подтверждения или «нет» для отмены (15 минут)."
CONFIRM = frozenset({"да", "подтверждаю", "да, подтверждаю", "всё верно", "все верно"})
CANCEL = frozenset({"нет", "отмена", "не надо", "отменить"})
OFF_TOPIC_REPLY = (
    "Я помогаю только с задачами, встречами и напоминаниями. Спросите о своих планах или добавьте запись."
)
QUOTED = re.compile(r"```.*?```|«[^»]*»|\"[^\"]*\"|'[^']*'|<[^>]*>.*?</[^>]*>", re.S)
DATA = re.compile(
    r"перев[ео]д|переведи|перескаж|проанализ|объясни|поясни|расшифруй|декодир|цитат|"
    r"не\s+(?:выполняй|исполняй|создавай|меняй|удаляй)|только\s+(?:текст|данные)|"
    r"run_shell|execute_sql|read_file|\.env|системн\w*\s+(?:промпт|инструкц)|"
    r"игнорируй\s+(?:правила|инструкц|предыдущ)|режим\s+разработчика",
    re.I,
)
READ = re.compile(
    r"\b(?:найди|покажи|поищи|поиск|скажи|расскажи|прочитай|перечисли|выведи|проверь)\b|"
    r"^\s*(?:а\s+)?(?:список|вывод|когда|какие|что\b|во сколько|есть ли|сколько|почему|зачем|где|как\b)|"
    r"^\s*(?:(?:мои|все)\s+)?(?:задачи|встречи|напоминания|планы|расписание)\b",
    re.I,
)
NEGATIVE = re.compile(
    r"\bне\s+(?:удал\w*|отмен\w*|меня\w*|измен\w*|созда\w*|перенос\w*|надо|нужно|хочу|следует|стоит)", re.I
)
DELETE = re.compile(r"\b(?:удали\w*|отмени\w*|убери|сотри|вычеркни)\b", re.I)
UPDATE = re.compile(
    r"\b(?:измени\w*|поменя\w*|переимен\w*|исправ\w*|перенес\w*|передвин\w*|сдвин\w*)\b", re.I
)
CREATE = re.compile(
    r"^\s*(?:(?:пожалуйста|можешь|можно|мне)\s*[, ]\s*)?"
    r"(?:созда\w*|добав\w*|запиш\w*|напом\w*|поставь|нужно|надо|сделай|сохрани|заведи|запланируй)\b",
    re.I,
)
ENTITY = re.compile(
    r"\b(?:задач\w*|встреч\w*|напоминан\w*|созвон\w*|план[её]рк\w*|лекци\w*|тренировк\w*|совещан\w*|пара|заняти\w*|при[её]м\w*|обед\w*|звонок\w*)\b",
    re.I,
)


def allowed_mutations(text: str) -> frozenset[str]:
    """Data processing/read requests are read-only even if their contents contain commands.

    No short reply or keyword inside a quoted title grants write access. Changes
    additionally require a concrete confirmation in Assistant; these are capabilities,
    not authorization to execute arbitrary arguments.
    """
    plain = QUOTED.sub(" ", text)
    question = "?" in plain and not (CREATE.search(plain) or UPDATE.search(plain) or DELETE.search(plain))
    if DATA.search(plain) or READ.search(plain) or NEGATIVE.search(plain) or question:
        return frozenset()
    result: set[str] = set()
    if DELETE.search(plain):
        result.add("delete_event")
    if UPDATE.search(plain):
        result.update(("update_event", "reschedule_meeting"))
    entity = ENTITY.search(plain)
    prefix = plain[: entity.start()].strip() if entity else ""
    declaration = bool(entity) and (
        not prefix
        or bool(
            re.match(
                r"^(?:сегодня|завтра|послезавтра|у меня|"
                r"в\s+(?:\d|понедельник|вторник|сред|четверг|пятниц|суббот|воскрес))\b",
                prefix,
                re.I,
            )
        )
    )
    if CREATE.search(plain) or (not result and declaration):
        result.add("create_event")
    return frozenset(result)


def confirmation_reply(text: str) -> bool:
    return text.casefold().strip().rstrip(".! ") in CONFIRM


def short_clarification(text: str) -> bool:
    plain = QUOTED.sub(" ", text)
    return (
        bool(plain.strip()) and len(plain.split()) <= 8 and not DATA.search(plain) and not READ.search(plain)
    )


PLANS = re.compile(
    r"\bплан(?:ы|ов|ам|ах|у|е|ом)?\b|запланир|расписан|календар|"
    r"\bдел(?:а|ам|ах)?\b|сегодня|завтра|послезавтра|недел|месяц|срок",
    re.I,
)
QUESTION = re.compile(
    r"\?|\b(?:что|кто|как|сколько|почему|зачем|где|откуда|расскажи|объясни|поясни|напиши|переведи)\b",
    re.I,
)
CONTEXTUAL_READ = re.compile(
    r"^\s*(?:а\s+)?(?:во сколько\b|когда\s+(?:она|он|оно|они|нач|зак|будет)|"
    r"сколько\s+(?:(?:она|он|оно|они)\s+)?длит)|"
    r"^\s*(?:найди|покажи|поищи|перечисли|выведи)\b",
    re.I,
)


def off_topic(text: str) -> bool:
    """Reject unrelated questions; short context replies still reach the stateful agent.

    A topic inside a quoted record title remains valid (e.g. a task about Python).
    No topic decision grants permission to write.
    """
    return not (
        ENTITY.search(text) or PLANS.search(text) or CONTEXTUAL_READ.search(text) or allowed_mutations(text)
    ) and bool(QUESTION.search(text))


def voice_requires_confirmation(text: str) -> bool:
    # Only clearly identified reads bypass the transcript check. Ambiguous fragments
    # may complete an earlier write request, so retain confirmation for them.
    if off_topic(text):
        return False
    return bool(allowed_mutations(text)) or not bool(READ.search(text))
