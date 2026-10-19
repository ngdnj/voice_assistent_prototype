"""Eval cases from the assignment, plus paraphrases and edge cases.

All cases run at a fixed moment: Monday 2026-10-19 12:00 Moscow time, so
"завтра" is 2026-10-20, "в пятницу" is 2026-10-23, the week ends 2026-10-25.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from evals.checks import Expect, at, covers, equals, mentions, on_date, types_include

TZ = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 19, 12, 0, 0, tzinfo=TZ)


@dataclass(frozen=True)
class SeedEvent:
    event_type: str
    name: str
    description: str = ""
    when: str | None = None


@dataclass(frozen=True)
class Case:
    id: str
    category: str
    turns: list[str]
    expect: Expect
    seed: list[SeedEvent] = field(default_factory=list)


SEED = [
    SeedEvent(
        "task",
        "Подготовить презентацию",
        "слайды для клиента по итогам квартала",
        "2026-10-21T18:00:00+03:00",
    ),
    SeedEvent("meeting", "Встреча с Иваном", "обсудить бюджет на 2027 год", "2026-10-12T14:00:00+03:00"),
    SeedEvent("task", "Проверить договор аренды", "юрист прислал правки", None),
    SeedEvent("meeting", "Созвон с командой", "еженедельный синк", "2026-10-19T16:00:00+03:00"),
    SeedEvent("reminder", "Оплатить интернет", "", "2026-10-19T20:00:00+03:00"),
    SeedEvent("task", "Купить подарок маме", "", "2026-10-20T19:00:00+03:00"),
]

TODAY = covers("2026-10-19T00:00:00+03:00", "2026-10-19T23:59:59+03:00", max_extra=timedelta(hours=1))
TOMORROW = covers("2026-10-20T00:00:00+03:00", "2026-10-20T23:59:59+03:00", max_extra=timedelta(hours=1))

CASES: list[Case] = [
    # 1. Tasks and reminders
    Case(
        "remind_tomorrow_10",
        "create",
        ["Напомни мне завтра в 10 утра позвонить Ивану"],
        Expect(
            "create_event",
            [
                equals("event_type", "reminder"),
                at("datetime", "2026-10-20T10:00:00+03:00"),
                mentions("name", "иван"),
            ],
        ),
    ),
    Case(
        "task_friday",
        "create",
        ["В пятницу нужно отправить отчет"],
        Expect(
            "create_event",
            [equals("event_type", "task"), on_date("datetime", "2026-10-23"), mentions("name", "отчет")],
        ),
    ),
    Case(
        "task_end_of_week",
        "create",
        ["Поставь задачу купить билеты до конца недели"],
        Expect(
            "create_event",
            [equals("event_type", "task"), on_date("datetime", "2026-10-25"), mentions("name", "билет")],
        ),
    ),
    Case(
        "remind_in_two_hours",
        "create",
        ["Напомни через два часа выпить таблетки"],
        Expect(
            "create_event", [equals("event_type", "reminder"), at("datetime", "2026-10-19T14:00:00+03:00")]
        ),
    ),
    Case(
        "task_day_after_tomorrow",
        "create",
        ["Послезавтра надо забрать посылку"],
        Expect("create_event", [equals("event_type", "task"), on_date("datetime", "2026-10-21")]),
    ),
    Case(
        "task_no_date",
        "create",
        ["Добавь задачу разобрать почту"],
        Expect("create_event", [equals("event_type", "task"), mentions("name", "почт")]),
    ),
    # 2. Meetings
    Case(
        "meeting_tomorrow_15",
        "create",
        ["Добавь встречу с командой завтра в 15:00"],
        Expect(
            "create_event",
            [
                equals("event_type", "meeting"),
                at("datetime", "2026-10-20T15:00:00+03:00"),
                mentions("name", "команд"),
            ],
        ),
    ),
    Case(
        "meeting_thursday_11",
        "create",
        ["В четверг в 11 у меня встреча с дизайнером"],
        Expect(
            "create_event",
            [
                equals("event_type", "meeting"),
                at("datetime", "2026-10-22T11:00:00+03:00"),
                mentions("name", "дизайнер"),
            ],
        ),
    ),
    Case(
        "meeting_next_week_monday",
        "create",
        ["Поставь созвон с бухгалтерией в понедельник на следующей неделе в 9:30"],
        Expect(
            "create_event", [equals("event_type", "meeting"), at("datetime", "2026-10-26T09:30:00+03:00")]
        ),
    ),
    # 3. Clarifying dialog
    Case(
        "meeting_without_time_asks",
        "clarify",
        ["Поставь встречу с Иваном завтра"],
        Expect(None, answer_mentions=["врем"]),
    ),
    Case(
        "meeting_without_time_then_time",
        "clarify",
        ["Поставь встречу с Иваном завтра", "В 16:00"],
        Expect(
            "create_event",
            [
                equals("event_type", "meeting"),
                at("datetime", "2026-10-20T16:00:00+03:00"),
                mentions("name", "иван"),
            ],
        ),
    ),
    # 4. Period listing — paraphrases must map to the same action
    Case("today_what", "list", ["Что у меня сегодня?"], Expect("get_event", [TODAY]), seed=SEED),
    Case("today_plans", "list", ["Какие планы на сегодня?"], Expect("get_event", [TODAY]), seed=SEED),
    Case(
        "today_meetings",
        "list",
        ["Покажи мои встречи на сегодня"],
        Expect("get_event", [TODAY, types_include("meeting")]),
        seed=SEED,
    ),
    Case(
        "today_any_meetings",
        "list",
        ["Есть ли у меня встречи сегодня?"],
        Expect("get_event", [TODAY, types_include("meeting")], answer_mentions=["команд"]),
        seed=SEED,
    ),
    Case(
        "tomorrow_tasks",
        "list",
        ["Какие задачи у меня на завтра?"],
        Expect("get_event", [TOMORROW, types_include("task")], answer_mentions=["подар"]),
        seed=SEED,
    ),
    Case(
        "this_week",
        "list",
        ["Что запланировано на эту неделю?"],
        Expect(
            "get_event",
            [covers("2026-10-19T12:00:00+03:00", "2026-10-25T23:59:59+03:00", max_extra=timedelta(hours=13))],
        ),
        seed=SEED,
    ),
    Case(
        "next_week",
        "list",
        ["Что у меня на следующей неделе?"],
        Expect(
            "get_event",
            [covers("2026-10-26T00:00:00+03:00", "2026-11-01T23:59:59+03:00", max_extra=timedelta(hours=1))],
        ),
        seed=SEED,
    ),
    # 5. Search of old records
    Case(
        "search_presentation",
        "search",
        ["Найди задачу про презентацию"],
        Expect("get_event_by_query", [mentions("text", "презентац")], answer_mentions=["презентац"]),
        seed=SEED,
    ),
    Case(
        "search_meeting_ivan",
        "search",
        ["Когда у меня была встреча с Иваном?"],
        Expect("get_event_by_query", [mentions("text", "иван")], answer_mentions=["12"]),
        seed=SEED,
    ),
    Case(
        "search_contract",
        "search",
        ["Я когда-то ставил задачу по договору, найди ее"],
        Expect("get_event_by_query", [mentions("text", "договор")], answer_mentions=["договор"]),
        seed=SEED,
    ),
    # 6. Small talk — no tools
    Case("smalltalk_hello", "smalltalk", ["Привет! Что ты умеешь?"], Expect(None)),
]
