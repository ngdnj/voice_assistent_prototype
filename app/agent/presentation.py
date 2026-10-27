"""Compact chat lists built only from records returned by the backend."""

from datetime import date, datetime
from zoneinfo import ZoneInfo

from app.mcp_server.schemas import EventDTO, sort_events
from app.planner.query import RecordSort

MONTHS = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
HEADINGS = {"meeting": "Встречи", "reminder": "Напоминания", "task": "Задачи"}
TYPES = {"meeting": "встреча", "reminder": "напоминание", "task": "задача"}
SORT_LABELS = {
    "time_desc": "сначала дальние даты",
    "duration_asc": "сначала короткие",
    "duration_desc": "сначала длинные",
    "name_asc": "по названию А–Я",
    "name_desc": "по названию Я–А",
    "created_asc": "сначала добавленные раньше",
    "created_desc": "сначала недавно добавленные",
}
MAX_ITEMS = 20
MAX_CHARS = 3800  # Leave room within MAX's 4000-character limit.


def _day(value: date, today: date) -> str:
    relative = (value - today).days
    if relative in (-1, 0, 1):
        return {-1: "Вчера", 0: "Сегодня", 1: "Завтра"}[relative]
    year = f" {value.year}" if value.year != today.year else ""
    return f"{value.day} {MONTHS[value.month - 1]}{year}"


def _duration(event: EventDTO) -> str:
    if event.event_type != "meeting" or event.datetime is None or event.end_time is None:
        return "длительность не указана"
    seconds = int((event.end_time - event.datetime).total_seconds())
    hours, rest = divmod(seconds, 3600)
    minutes, seconds = divmod(rest, 60)
    return " ".join(
        part
        for part in (
            f"{hours} ч" if hours else "",
            f"{minutes} мин" if minutes else "",
            f"{seconds} с" if seconds else "",
        )
        if part
    )


def _when(event: EventDTO, today: date, zone: ZoneInfo) -> str:
    if event.datetime is None:
        return "Без срока" if event.date is None else f"{_day(event.date, today)}, без времени"
    start = event.datetime.astimezone(zone)
    value = f"{_day(start.date(), today)}, {start:%H:%M}"
    if event.event_type == "meeting" and event.end_time is not None:
        end = event.end_time.astimezone(zone)
        if end.date() == start.date():
            value += f"–{end:%H:%M}"
        else:
            value += f" — {_day(end.date(), today).lower()}, {end:%H:%M}"
    return value


def format_event_list(
    events: list[EventDTO],
    *,
    reference_time: datetime,
    timezone: ZoneInfo,
    sort: RecordSort = "time_asc",
    truncated: bool = False,
) -> str:
    if not events:
        return "Ничего не найдено."
    events = sort_events(events, sort, timezone.key)
    kinds = {event.event_type for event in events}
    heading = HEADINGS[next(iter(kinds))] if len(kinds) == 1 else "Планы"
    if sort in SORT_LABELS:
        heading += f" · {SORT_LABELS[sort]}"
    lines = [heading + ":"]
    today = reference_time.astimezone(timezone).date()
    shown = 0
    for event in events[:MAX_ITEMS]:
        # A title is untrusted data: keep it on its own numbered line, without markup.
        title = " ".join(event.name.split())
        kind = f" ({TYPES[event.event_type]})" if len(kinds) > 1 else ""
        duration = (
            f" · {_duration(event)}" if sort.startswith("duration_") and event.event_type == "meeting" else ""
        )
        line = f"{shown + 1}) {_when(event, today, timezone)} — {title}{kind}{duration}"
        if sum(len(item) + 1 for item in lines) + len(line) > MAX_CHARS - 100:
            break
        lines.append(line)
        shown += 1
    if truncated or shown < len(events):
        lines.extend(["", f"Показано: {shown}. Уточните период или поиск."])
    return "\n".join(lines)
