"""REST records API for the MAX Mini App; same operations and DTOs as the MCP tools."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Annotated, Any
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse

from app.api.init_data import InvalidInitDataError, verify_init_data
from app.mcp_server.context import TrustedContext
from app.mcp_server.metrics import measured
from app.mcp_server.schemas import error
from app.mcp_server.tools import EventTools

INIT_DATA_HEADER = "X-Max-Init-Data"
INIT_DATA_MAX_AGE = timedelta(days=1)

# Structured error codes map to HTTP statuses; the body keeps the MCP error format.
STATUS_BY_CODE = {
    "validation_error": 422,
    "not_found": 404,
    "operation_conflict": 409,
    "temporary_unavailable": 503,
    "unauthorized": 401,
}


def _respond(result: dict[str, Any]) -> JSONResponse:
    if "error" in result:
        return JSONResponse(result, status_code=STATUS_BY_CODE[result["error"]["code"]])
    return JSONResponse(result)


def build_records_router(
    tools: EventTools,
    *,
    bot_token: str,
    clock: Callable[[], datetime],
    max_age: timedelta = INIT_DATA_MAX_AGE,
) -> APIRouter:
    """Routes under /api; the owner comes only from initData verified with the bot token."""
    router = APIRouter(prefix="/api")

    def owner_id(
        init_data: Annotated[str | None, Header(alias=INIT_DATA_HEADER)] = None,
    ) -> str | None:
        if init_data is None:
            return None
        try:
            return verify_init_data(init_data, bot_token=bot_token, now=clock(), max_age=max_age).owner_id
        except InvalidInitDataError:
            return None

    # Parameters are optional for FastAPI so that a call without valid initData always gets 401
    # first; EventTools then validates them exactly as for MCP and answers with a structured 422.
    Owner = Annotated[str | None, Depends(owner_id)]
    Text = Annotated[str | None, Query()]
    EventTypes = Annotated[list[str] | None, Query()]

    def context(owner: str | None, timezone: str | None) -> TrustedContext | dict[str, Any]:
        if owner is None:
            return error("unauthorized", "Откройте приложение из MAX заново", None)
        # The time zone is a display setting of the client (IANA, e.g. from Intl); it never affects access.
        try:
            if timezone is None or len(timezone) > 64:
                raise ValueError("timezone")
            ZoneInfo(timezone)
        except (ZoneInfoNotFoundError, ValueError):
            return error("validation_error", "Неизвестный часовой пояс", "timezone")
        return TrustedContext(owner_id=owner, timezone=timezone, request_id=str(uuid4()), action_id=None)

    async def respond(
        operation: str,
        owner: str | None,
        timezone: str | None,
        call: Callable[[TrustedContext], Awaitable[dict[str, Any]]],
    ) -> JSONResponse:
        async def guarded() -> dict[str, Any]:
            caller = context(owner, timezone)
            return caller if isinstance(caller, dict) else await call(caller)

        return _respond(await measured(operation, "rest", guarded))

    @router.get("/events")
    async def events_by_period(
        owner: Owner,
        start_time: Text = None,
        end_time: Text = None,
        timezone: Text = None,
        event_types: EventTypes = None,
    ) -> JSONResponse:
        arguments = {"start_time": start_time, "end_time": end_time, "event_types": event_types}
        return await respond(
            "get_events_by_period",
            owner,
            timezone,
            lambda caller: tools.get_events_by_period(caller, arguments),
        )

    @router.get("/events/search")
    async def search_events(
        owner: Owner,
        query: Text = None,
        timezone: Text = None,
        event_types: EventTypes = None,
        start_time: Text = None,
        end_time: Text = None,
        limit: Text = None,
    ) -> JSONResponse:
        arguments = {
            "query": query,
            "event_types": event_types,
            "start_time": start_time,
            "end_time": end_time,
            # ASCII digits only: "²".isdigit() is True, but int("²") fails.
            "limit": int(limit) if limit is not None and limit.isascii() and limit.isdigit() else limit,
        }
        return await respond(
            "get_event_by_query", owner, timezone, lambda caller: tools.get_event_by_query(caller, arguments)
        )

    @router.patch("/tasks/{task_id}/status")
    async def change_task_status(owner: Owner, task_id: str, request: Request) -> JSONResponse:
        async def change(caller: TrustedContext) -> dict[str, Any]:
            # The body is read only after initData is verified, so a bad body never precedes 401.
            try:
                body = await request.json()
            except ValueError:
                body = None
            status = body.get("status") if isinstance(body, dict) else None
            return await tools.update_task_status(caller, task_id, status)

        # A status change involves no local dates, so the zone does not matter here.
        return await respond("update_task_status", owner, "UTC", change)

    return router
