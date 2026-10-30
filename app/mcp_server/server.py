"""FastMCP tools; the model never chooses the owner or the action ID."""

from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastmcp import FastMCP
from fastmcp.exceptions import ValidationError as ToolArgumentsError
from fastmcp.server.dependencies import get_http_headers
from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from fastmcp.tools import ToolResult
from mcp import types as mt
from pydantic import Field, ValidationError

from app.mcp_server.context import TrustedContext, UnauthorizedError, context_from_headers
from app.mcp_server.metrics import measured
from app.mcp_server.schemas import error
from app.mcp_server.tools import EventTools
from app.planner.query import RECORD_SORTS

ContextProvider = Callable[[], TrustedContext]

INSTRUCTIONS = (
    "Инструменты задач, встреч и напоминаний пользователя. Владельца, часовой пояс и идентификатор "
    "действия задаёт сервер. Время — ISO 8601 с UTC или смещением; периоды — полуинтервалы [start, end). "
    'Ошибка возвращается как {"error": {"code", "message", "field"}}; пустая выдача — {"events": []}.'
)

# Enumerations are shown to the model in the schema but checked by EventTools, so that a wrong
# value becomes a structured validation_error instead of a transport-level failure.
EventTypeArg = Annotated[
    str,
    Field(
        description="task, meeting или reminder", json_schema_extra={"enum": ["task", "meeting", "reminder"]}
    ),
]
EventTypesArg = Annotated[
    list[Annotated[str, Field(json_schema_extra={"enum": ["task", "meeting", "reminder"]})]] | None,
    Field(description="Фильтр типов: task, meeting, reminder. По умолчанию все"),
]
MomentArg = Annotated[
    str | None, Field(description="ISO 8601 с UTC или смещением, например 2026-10-24T15:00:00+03:00")
]
RangeEndArg = Annotated[
    str | None,
    Field(
        description="Явно указанное окончание интервала задачи или встречи, "
        "ISO 8601 со смещением; позже datetime"
    ),
]
SortArg = Annotated[
    str | None,
    Field(
        description=(
            "Порядок: time_asc (по умолчанию), time_desc; duration_asc/duration_desc — "
            "сначала короткие/длинные встречи, неизвестная длительность в конце; "
            "name_asc/name_desc — название; created_asc/created_desc — время добавления"
        ),
        json_schema_extra={"enum": list(RECORD_SORTS)},
    ),
]


class StructuredArgumentErrors(Middleware):
    """Arguments that fail the tool schema (missing name, wrong type, extra user_id) become the same
    structured validation_error as other mistakes, instead of a transport error with pydantic text."""

    async def on_call_tool(
        self,
        context: MiddlewareContext[mt.CallToolRequestParams],
        call_next: CallNext[mt.CallToolRequestParams, ToolResult],
    ) -> ToolResult:
        try:
            return await call_next(context)
        except ToolArgumentsError as exc:
            cause = exc.__cause__
            location = (
                cause.errors()[0]["loc"] if isinstance(cause, ValidationError) and cause.errors() else ()
            )
            field = str(location[0]) if location else None
            result = error("validation_error", "Неверные аргументы инструмента", field)
            return ToolResult(structured_content=result)


def header_context(service_token: str) -> ContextProvider:
    """Read the trusted context from the current HTTP request of the agent worker."""

    def provide() -> TrustedContext:
        return context_from_headers(get_http_headers(include={"authorization"}), service_token=service_token)

    return provide


def build_mcp_server(tools: EventTools, context: ContextProvider) -> FastMCP:
    """Assemble the server without network access; the caller runs or mounts it."""
    mcp = FastMCP("it-gorky-events", instructions=INSTRUCTIONS, mask_error_details=True)
    mcp.add_middleware(StructuredArgumentErrors())

    async def run(
        operation: str, call: Callable[[TrustedContext], Awaitable[dict[str, Any]]]
    ) -> dict[str, Any]:
        """Resolve the trusted context, call the tool logic and record metrics."""

        async def guarded() -> dict[str, Any]:
            try:
                caller = context()
            except UnauthorizedError:
                return error("unauthorized", "Вызов без доверенного контекста пользователя отклонён", None)
            return await call(caller)

        return await measured(operation, "mcp", guarded)

    @mcp.tool(
        description=(
            "Создать задачу, встречу или напоминание. "
            "Задача: datetime или date (срок) необязательны, не вместе. "
            "Задача с интервалом: datetime — начало, end_time — явно названное окончание. "
            "Встреча: обязательно datetime начала, end_time — только если пользователь назвал окончание "
            "(длительность не придумывай). Напоминание: обязательно datetime. Если времени нет — уточни у "
            'пользователя, а не вызывай инструмент. Результат: {"created": запись} или {"error": ...}.'
        )
    )
    async def create_event(
        event_type: EventTypeArg,
        name: Annotated[str, Field(description="Короткое название, 1–200 символов")],
        description: Annotated[str | None, Field(description="Подробности, до 2000 символов")] = None,
        date: Annotated[str | None, Field(description="Срок задачи без времени, YYYY-MM-DD")] = None,
        datetime: MomentArg = None,
        end_time: RangeEndArg = None,
    ) -> dict[str, Any]:
        return await run(
            "create_event",
            lambda caller: tools.create_event(
                caller,
                {
                    "event_type": event_type,
                    "name": name,
                    "description": description,
                    "date": date,
                    "datetime": datetime,
                    "end_time": end_time,
                },
            ),
        )

    @mcp.tool(
        description=(
            "Записи пользователя за период [start_time, end_time), до 366 дней, не больше 100 записей "
            "(truncated=true — есть ещё). Задачи выбираются по сроку; задачи без срока сюда не входят. "
            "Интервалы задач и встреч — по пересечению с периодом, без конца — по началу. "
            'Напоминания — по времени. Пустой период — {"events": []}.'
        )
    )
    async def get_events_by_period(
        start_time: Annotated[str, Field(description="Начало периода включительно, ISO 8601 со смещением")],
        end_time: Annotated[str, Field(description="Конец периода не включительно, ISO 8601 со смещением")],
        event_types: EventTypesArg = None,
        sort: SortArg = None,
        limit: Annotated[int | None, Field(description="Число записей, 1–100, по умолчанию 100")] = None,
    ) -> dict[str, Any]:
        return await run(
            "get_events_by_period",
            lambda caller: tools.get_events_by_period(
                caller,
                {
                    "start_time": start_time,
                    "end_time": end_time,
                    "event_types": event_types,
                    "sort": sort,
                    "limit": limit,
                },
            ),
        )

    @mcp.tool(
        description=(
            "Найти записи по словам из названия или описания (полнотекстовый поиск с русской морфологией, "
            "не по смыслу). Можно сузить типами и периодом. Возвращает найденные записи с id; "
            'ничего не найдено — {"events": []}.'
        )
    )
    async def get_event_by_query(
        query: Annotated[str, Field(description="Слова для поиска, 1–200 символов")],
        event_types: EventTypesArg = None,
        start_time: MomentArg = None,
        end_time: MomentArg = None,
        limit: Annotated[
            int | None, Field(description="Сколько записей вернуть, 1–50, по умолчанию 10")
        ] = None,
        sort: SortArg = None,
    ) -> dict[str, Any]:
        return await run(
            "get_event_by_query",
            lambda caller: tools.get_event_by_query(
                caller,
                {
                    "query": query,
                    "event_types": event_types,
                    "start_time": start_time,
                    "end_time": end_time,
                    "limit": limit,
                    "sort": sort,
                },
            ),
        )

    @mcp.tool(
        description=(
            "Изменить время существующей встречи, не создавая новую. Сначала найди id поиском или в истории; "
            "если совпадений несколько — уточни выбор. meeting_id — UUID встречи. datetime — новое начало, "
            "end_time — новое окончание; хотя бы одно обязательно. Только начало сохраняет известную "
            "длительность; неизвестное окончание не назначается. Только окончание сохраняет начало. "
            'Результат: {"updated": запись} или {"error": ...}. Подтверждай перенос только после updated.'
        )
    )
    async def reschedule_meeting(
        meeting_id: Annotated[
            str, Field(description="id существующей встречи из результата поиска или истории")
        ],
        datetime: MomentArg = None,
        end_time: MomentArg = None,
    ) -> dict[str, Any]:
        return await run(
            "reschedule_meeting",
            lambda caller: tools.reschedule_meeting(
                caller,
                {
                    "meeting_id": meeting_id,
                    "datetime": datetime,
                    "end_time": end_time,
                },
            ),
        )

    @mcp.tool(
        description=(
            "Изменить название и/или время существующей задачи, встречи или напоминания. "
            "Сначала найди запись поиском или в истории; при нескольких совпадениях уточни выбор. "
            "Не создаёт новую запись. name — новое название; datetime — новое начало встречи/время "
            "напоминания/срок задачи; date — только дата задачи, не вместе с datetime; end_time — "
            "окончание задачи или встречи. Новое начало сохраняет известную длительность "
            "при пропуске end_time. "
            'Нужно хотя бы одно изменение. Успех: {"updated": запись}, ошибка: {"error": ...}.'
        )
    )
    async def update_event(
        event_id: Annotated[str, Field(description="UUID записи из поиска/истории; не придумывай ID")],
        name: Annotated[str | None, Field(description="Новое название, 1–200 символов")] = None,
        datetime: MomentArg = None,
        date: Annotated[str | None, Field(description="Новый срок задачи без времени, YYYY-MM-DD")] = None,
        end_time: RangeEndArg = None,
    ) -> dict[str, Any]:
        return await run(
            "update_event",
            lambda caller: tools.update_event(
                caller,
                {
                    "event_id": event_id,
                    "name": name,
                    "datetime": datetime,
                    "date": date,
                    "end_time": end_time,
                },
            ),
        )

    @mcp.tool(
        description=(
            "Удалить одну существующую запись по явной просьбе пользователя. Найди её ID поиском или "
            "в истории; при нескольких совпадениях сначала уточни выбор. Запись исчезает из доступных "
            'списков и поиска. Подтверждай только после {"deleted": запись}; ошибка: {"error": ...}.'
        )
    )
    async def delete_event(
        event_id: Annotated[str, Field(description="UUID записи из поиска/истории; не придумывай ID")],
    ) -> dict[str, Any]:
        return await run("delete_event", lambda caller: tools.delete_event(caller, {"event_id": event_id}))

    return mcp
