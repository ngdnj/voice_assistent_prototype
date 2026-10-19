"""MCP server exposing the user's tasks, reminders and meetings to the LLM agent.

Tool names, parameters and descriptions are the contract the agent relies on;
descriptions are written for the LLM, so they are tuned together with the prompt.
"""

from datetime import datetime as dt
from typing import Annotated, Any
from zoneinfo import ZoneInfo

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from assistant.config import get_settings
from assistant.events.models import TYPES_REQUIRING_DATETIME, EventType
from assistant.events.storage import EventStorage

SERVER_INSTRUCTIONS = (
    "Хранилище личных задач, напоминаний и встреч пользователя. "
    "Все даты и время передаются строками ISO 8601 с часовым поясом, "
    "например 2026-10-20T15:00:00+03:00. Периоды задаются диапазоном start_time/end_time."
)

UserId = Annotated[str, Field(description="Идентификатор пользователя. Подставляется системой.")]
IsoDatetime = Annotated[
    str,
    Field(description="Дата и время в ISO 8601 с часовым поясом, например 2026-10-20T15:00:00+03:00."),
]


def _parse_iso(value: str, tz: ZoneInfo, field: str) -> dt:
    try:
        parsed = dt.fromisoformat(value)
    except ValueError as exc:
        raise ToolError(f"{field}: ожидается ISO 8601, получено {value!r}") from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=tz)


def build_server(storage: EventStorage, tz: ZoneInfo) -> FastMCP:
    mcp = FastMCP("assistant-events", instructions=SERVER_INSTRUCTIONS)

    @mcp.tool(
        description=(
            "Создать запись пользователя: задачу (task), напоминание (reminder) или встречу (meeting). "
            "Для reminder и meeting время обязательно — если пользователь его не назвал, "
            "не вызывай инструмент, а спроси время. Для задачи без времени, но с датой или сроком "
            "передай конец этого дня (23:59:00). Для задачи без даты передай null."
        )
    )
    def create_event(
        user_id: UserId,
        event_type: Annotated[
            EventType, Field(description="task — дело/задача, reminder — напоминание, meeting — встреча.")
        ],
        name: Annotated[str, Field(description="Короткое название, например «Позвонить Ивану».")],
        description: Annotated[
            str,
            Field(description="Подробности своими словами: с кем, о чём. Используется для поиска."),
        ] = "",
        datetime: Annotated[
            str | None,
            Field(description="Момент события или срок задачи в ISO 8601 с часовым поясом."),
        ] = None,
    ) -> dict[str, Any]:
        if not name.strip():
            raise ToolError("name не может быть пустым")
        when = _parse_iso(datetime, tz, "datetime") if datetime else None
        if when is None and event_type in TYPES_REQUIRING_DATETIME:
            raise ToolError(f"Для {event_type.value} нужно время. Уточни его у пользователя.")
        event = storage.create(user_id, event_type, name.strip(), description.strip(), when)
        return {"created": event.to_dict()}

    @mcp.tool(
        description=(
            "Получить записи пользователя за период [start_time, end_time]. "
            "Используй для вопросов «что у меня сегодня», «какие задачи на завтра», "
            "«что запланировано на неделю». Для «сегодня» передай весь день с 00:00:00 до 23:59:59."
        )
    )
    def get_event(
        user_id: UserId,
        event_type: Annotated[
            list[EventType],
            Field(description="Какие типы вернуть. Пустой список — все типы."),
        ],
        start_time: IsoDatetime,
        end_time: IsoDatetime,
    ) -> dict[str, Any]:
        start = _parse_iso(start_time, tz, "start_time")
        end = _parse_iso(end_time, tz, "end_time")
        if start > end:
            raise ToolError("start_time позже end_time")
        events = storage.list_between(user_id, event_type, start, end)
        return {"count": len(events), "events": [e.to_dict() for e in events]}

    @mcp.tool(
        description=(
            "Найти ранее созданные записи по смыслу запроса, например «задача про презентацию», "
            "«встреча с Иваном», «договор». В text передай ключевые слова без служебных слов. "
            "Период start_time/end_time необязателен — укажи его, только если пользователь назвал время."
        )
    )
    def get_event_by_query(
        user_id: UserId,
        text: Annotated[str, Field(description="Ключевые слова для поиска, например «Иван встреча».")],
        start_time: Annotated[str | None, Field(description="Начало периода в ISO 8601 или null.")] = None,
        end_time: Annotated[str | None, Field(description="Конец периода в ISO 8601 или null.")] = None,
    ) -> dict[str, Any]:
        start = _parse_iso(start_time, tz, "start_time") if start_time else None
        end = _parse_iso(end_time, tz, "end_time") if end_time else None
        events = storage.search(user_id, text, start, end)
        return {"count": len(events), "events": [e.to_dict() for e in events]}

    return mcp


def main() -> None:
    settings = get_settings()
    storage = EventStorage(settings.storage_path, settings.tz)
    build_server(storage, settings.tz).run(transport="http", host=settings.mcp_host, port=settings.mcp_port)


if __name__ == "__main__":
    main()
