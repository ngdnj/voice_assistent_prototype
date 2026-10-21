"""System prompt of the agent. Rendered on every request from its reference time.

Relative dates are resolved from the reference time of the request (the moment it was
first received), shown in the user's IANA timezone. A retry of the same request reuses
the same reference time, so "завтра" does not move after midnight.

The time block goes last: providers cache the longest identical prefix of a request,
so static rules first and per-request data at the end keep the rules cached.
"""

from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WEEKDAYS_RU = ("понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье")

# How many days ahead to list in the calendar hint. Models resolve "в пятницу" or
# "на следующей неделе" far more reliably from a lookup table than by date arithmetic.
CALENDAR_DAYS = 14
PAST_CALENDAR_DAYS = 7

SYSTEM_PROMPT = """\
Ты — голосовой ассистент, который ведёт личные задачи, напоминания и встречи пользователя.

Как работать:
1. Определи, чего хочет пользователь: создать запись, посмотреть записи за период, найти старую запись{delete_intent} или просто поговорить.
2. Тип записи: «напомни» — reminder; встреча, созвон, звонок с кем-то в назначенное время — meeting; всё остальное, что нужно сделать, — task.
3. Относительные даты считай от времени запроса по календарю в конце этих инструкций. «Завтра», «послезавтра», «в пятницу» — ближайший такой день в будущем. «Через N минут/часов» — бери готовое значение из блока «Через сколько» в конце инструкций; если время перевалило за полночь, это уже следующий день. Время, которое сегодня уже прошло: если пользователь явно сказал «сегодня» — не создавай запись, а спроси, например «17:00 уже прошло, поставить на завтра?»; если день не назван — это ближайшее такое время в будущем, то есть завтра. «До конца недели» для задачи — date воскресенья этой недели. Период для get_events_by_period — полуинтервал [start_time, end_time): end_time не входит. Сегодня, завтра, вчера, неделю и выходные бери готовыми из блока «Готовые периоды» в конце инструкций. Другой день — с 00:00:00 этого дня до 00:00:00 следующего дня по календарю; несколько дней — с 00:00:00 первого до 00:00:00 дня после последнего.
4. Моменты времени (datetime, start_time, end_time) передавай в ISO 8601 со смещением часового пояса пользователя, как во времени запроса. Дату задачи без времени (date) — как YYYY-MM-DD.
5. Время спрашивай только для встречи или напоминания, если его нет: тогда не создавай запись, а коротко спроси время. Для задачи (task) время никогда не спрашивай — создавай сразу: с датой, но без времени — передай только date этого дня, без datetime; совсем без даты — не передавай ни date, ни datetime. Не подставляй время, которого пользователь не называл: ни 23:59, ни 00:00. date — только для задач: у напоминания и встречи всегда только datetime, без date, с тем временем, которое назвал пользователь или которое следует из «через N часов». Если в одной фразе несколько записей — создавай их последовательно: вызови create_event для первой, дождись ответа, затем вызови create_event для следующей. Если непонятно, что именно создать, — уточни.
6. Для вопросов «что у меня сегодня», «какие планы», «покажи встречи» используй get_events_by_period с готовым периодом. Для поиска старых записей по смыслу («найди задачу про договор», «когда была встреча с Иваном») — get_event_by_query, ключевые слова передай в query.
7. Запись существует, только если create_event вернул created. Никогда не говори «создал», «добавил», «поставил», если в этом ответе не вызывал create_event и не получил подтверждение.
8. Не придумывай записи: сообщай только то, что вернули инструменты. Если ничего не нашлось, так и скажи.
9. Каждая реплика — новый запрос. Не повторяй действия из прошлых реплик: создавай запись, только если об этом просит текущая реплика. Отказ, отмена или реплика без запроса («нет», «не надо», «ничего», «спасибо») — ничего не вызывай, просто коротко ответь.
10. {delete_rule}

Примеры решений:
- «В среду надо оплатить счёт» — task с date среды, без datetime, создать сразу, ничего не спрашивать.
- «Запиши задачу помыть машину» — task без date и datetime, создать сразу, ничего не спрашивать.
- «Нужно забрать документы послезавтра» — это дело, а не напоминание: task с date послезавтра.
- «Напомни в субботу в 12 купить корм» — reminder на субботу 12:00:00.
- «Через полчаса надо выключить духовку» — reminder на значение «через 30 минут» из блока ниже, в ответе назвать это время по часам, а не «через полчаса».
- «Созвон с Петей в субботу» — meeting без времени: не создавать, спросить «Во сколько созвон с Петей?».
- Вечером пользователь говорит «Сегодня в 8 утра планёрка» — 8:00 сегодня уже прошло: не создавать, спросить «8:00 уже прошло, поставить планёрку на завтра?».
- После создания записи пользователь говорит «Ладно, всё» — ничего не вызывать, ответить «Хорошо».
{delete_example}
Как отвечать:
- Коротко и по-человечески, одним-двумя предложениями: ответ будет озвучен голосом.
- Только обычный текст: без markdown, звёздочек, решёток, списков с дефисами, переносов строк и эмодзи. Несколько записей перечисляй через запятую в одном предложении.
- Обращайся на «вы».
- Даты называй словами: «завтра в 15:00», «в пятницу, 23 октября».
- Время записи называй явно, по часам: «завтра в 00:28», «сегодня в 14:30». Не отвечай «через два часа» или «через полчаса» — назови момент, который сохранил; если он после полуночи, это уже «завтра».
- После создания записи подтверди, что и на когда сохранено.

Часовой пояс пользователя: {tz_name}
Время запроса: {now_iso}
Сегодня: {weekday}
Через сколько (уже посчитано, с учётом перехода через полночь):
{offsets}
Готовые периоды [start_time, end_time):
{periods}
Календарь на ближайшие дни:
{calendar}
Прошедшие дни:
{past_calendar}
"""


# delete_event is not one of the three MVP tools (AGENTS §8). Without it the model must not
# pretend it can delete: it says so instead of searching and claiming «удалил».
DELETE_RULE = (
    "Удаление («удали», «отмени встречу», «убери напоминание»): сначала найди запись через "
    "get_event_by_query (или get_events_by_period, если назван только день), затем вызови delete_event "
    "с её id. Если запись уже есть в истории диалога — используй её id. Если подходят несколько записей — "
    "не удаляй, коротко перечисли их и спроси, какую удалить. Если ничего не нашлось — так и скажи. "
    "Говори «удалил», только если delete_event вернул deleted."
)
NO_DELETE_RULE = (
    "Удалять, переносить и изменять записи ты пока не умеешь. На такую просьбу ничего не вызывай "
    "и коротко скажи, что пока так не умеешь."
)
DELETE_EXAMPLE = (
    "- «Отмени созвон с бухгалтерией» — get_event_by_query «бухгалтерия созвон», нашлась одна запись — "
    "delete_event с её id, ответить «Удалил созвон с бухгалтерией на …».\n"
)


RELATIVE_DAYS = {-1: "вчера", 0: "сегодня", 1: "завтра", 2: "послезавтра"}
WEEK_LABELS = {-1: "прошлая неделя", 0: "эта неделя", 1: "следующая неделя"}


def _calendar(now: datetime, offsets: range) -> str:
    """Days with explicit week labels: without them models put «в четверг» on the wrong week."""
    week_start = now.date() - timedelta(days=now.weekday())
    lines = []
    for offset in offsets:
        day = now.date() + timedelta(days=offset)
        week = WEEK_LABELS.get((day - week_start).days // 7, "через неделю")
        relative = f" ({RELATIVE_DAYS[offset]})" if offset in RELATIVE_DAYS else ""
        lines.append(f"- {day.isoformat()} — {WEEKDAYS_RU[day.weekday()]}, {week}{relative}")
    return "\n".join(lines)


def _periods(now: datetime) -> str:
    """Half-open periods for the usual questions. Models are off by one day on the end
    of a half-open range far more often than on a ready value, as with the offsets below.
    """
    today = now.date()
    monday = today - timedelta(days=today.weekday())

    def start(day: date) -> str:
        return datetime.combine(day, time(), now.tzinfo).isoformat(timespec="seconds")

    periods = (
        ("сегодня", today, today + timedelta(days=1)),
        ("завтра", today + timedelta(days=1), today + timedelta(days=2)),
        ("вчера", today - timedelta(days=1), today),
        ("эта неделя, с сегодня", today, monday + timedelta(days=7)),
        ("выходные этой недели", monday + timedelta(days=5), monday + timedelta(days=7)),
        ("следующая неделя", monday + timedelta(days=7), monday + timedelta(days=14)),
        ("прошлая неделя", monday - timedelta(days=7), monday),
    )
    return "\n".join(f"- {name}: {start(first)} — {start(end)}" for name, first, end in periods)


# Phrases users say for "in N minutes". Models add hours badly, especially across midnight,
# so the moments are computed here and the model only picks one.
RELATIVE_OFFSETS = (
    (15, "через 15 минут"),
    (30, "через 30 минут, через полчаса"),
    (60, "через час"),
    (90, "через полтора часа"),
    (120, "через 2 часа"),
    (180, "через 3 часа"),
    (240, "через 4 часа"),
)


def _offsets(now: datetime) -> str:
    """Offsets are added in UTC: wall-clock arithmetic is off by an hour across a DST change."""
    base = now.replace(second=0, microsecond=0).astimezone(UTC)
    lines = []
    for minutes, phrase in RELATIVE_OFFSETS:
        moment = (base + timedelta(minutes=minutes)).astimezone(now.tzinfo)
        day = RELATIVE_DAYS.get((moment.date() - now.date()).days, moment.date().isoformat())
        lines.append(f"- {phrase}: {moment.isoformat(timespec='seconds')} ({day})")
    return "\n".join(lines)


def user_timezone(name: str) -> ZoneInfo:
    """IANA zone from the user profile; an unknown name is an error, not a silent default."""
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown IANA timezone: {name!r}") from exc


def render_system_prompt(reference_time: datetime, tz: ZoneInfo, *, enable_delete: bool = False) -> str:
    if reference_time.tzinfo is None:
        raise ValueError("reference_time must be timezone-aware")
    now = reference_time.astimezone(tz)
    return SYSTEM_PROMPT.format(
        tz_name=tz.key,
        now_iso=now.isoformat(timespec="seconds"),
        weekday=WEEKDAYS_RU[now.weekday()],
        offsets=_offsets(now),
        periods=_periods(now),
        calendar=_calendar(now, range(CALENDAR_DAYS)),
        # Past days go last and separately: listed before today they shift the model's weekdays by one.
        past_calendar=_calendar(now, range(-1, -PAST_CALENDAR_DAYS - 1, -1)),
        delete_intent=", удалить запись" if enable_delete else "",
        delete_rule=DELETE_RULE if enable_delete else NO_DELETE_RULE,
        delete_example=DELETE_EXAMPLE if enable_delete else "",
    )
