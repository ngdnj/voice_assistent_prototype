"""Run role 4's HTTP process: MCP for the agent worker and REST for the Mini App.

`python -m app.mcp_server` serves /mcp (streamable HTTP) and /api/* on one port with PostgreSQL storage.
Until role 1 assembles the shared API (Q1 in the plan), this is the process to run.
"""

import asyncio
import logging
import os
from dataclasses import dataclass, field
from datetime import UTC, datetime

import uvicorn
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import make_asgi_app
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.api.records import INIT_DATA_HEADER, build_records_router
from app.mcp_server.server import build_mcp_server, header_context
from app.mcp_server.tools import EventTools
from app.planner.event_service import EventService
from app.planner.meeting_service import MeetingService
from app.planner.reminder_service import ReminderService
from app.planner.service import TaskService
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.event_mutations import PostgresEventMutations
from app.storage.postgres.meetings import PostgresMeetingRepository
from app.storage.postgres.reminders import PostgresReminderRepository
from app.storage.postgres.settings import PostgresSettings
from app.storage.postgres.tasks import PostgresTaskRepository

LOGGER = logging.getLogger("it_gorky.mcp")
MIN_TOKEN_LENGTH = 32


@dataclass(frozen=True)
class McpSettings:
    service_token: str = field(repr=False)
    host: str = "127.0.0.1"
    port: int = 8001
    path: str = "/mcp"
    # The Mini App REST API is enabled only with the bot token used to sign initData.
    bot_token: str | None = field(default=None, repr=False)
    mini_app_origins: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if len(self.service_token) < MIN_TOKEN_LENGTH:
            raise ValueError(f"MCP_SERVICE_TOKEN must contain at least {MIN_TOKEN_LENGTH} characters")
        if not 1 <= self.port <= 65535:
            raise ValueError("MCP_PORT must be between 1 and 65535")
        if not self.path.startswith("/") or self.path.startswith("/api"):
            raise ValueError("MCP_PATH must start with / and not with /api")

    @classmethod
    def from_env(cls) -> "McpSettings":
        origins = os.environ.get("MINI_APP_ORIGINS", "")
        return cls(
            service_token=os.environ.get("MCP_SERVICE_TOKEN", ""),
            host=os.environ.get("MCP_HOST", "127.0.0.1"),
            port=int(os.environ.get("MCP_PORT", "8001")),
            path=os.environ.get("MCP_PATH", "/mcp"),
            bot_token=os.environ.get("MAX_BOT_TOKEN") or None,
            mini_app_origins=tuple(origin.strip() for origin in origins.split(",") if origin.strip()),
        )


def clock() -> datetime:
    return datetime.now(UTC)


def build_tools(sessions: async_sessionmaker[AsyncSession]) -> EventTools:
    return EventTools(
        editor=EventService(repository=PostgresEventMutations(sessions), clock=clock),
        tasks=TaskService(repository=PostgresTaskRepository(sessions), clock=clock),
        meetings=MeetingService(repository=PostgresMeetingRepository(sessions), clock=clock),
        reminders=ReminderService(repository=PostgresReminderRepository(sessions), clock=clock),
    )


def build_app(tools: EventTools, settings: McpSettings) -> FastAPI:
    """One ASGI app: REST routes first, then the MCP app mounted at the root with its own path."""
    mcp_app = build_mcp_server(tools, header_context(settings.service_token)).http_app(path=settings.path)
    app = FastAPI(title="IT Gorky records", lifespan=mcp_app.lifespan, docs_url=None, redoc_url=None)
    if settings.bot_token is not None:
        app.include_router(build_records_router(tools, bot_token=settings.bot_token, clock=clock))
        if settings.mini_app_origins:
            app.add_middleware(
                CORSMiddleware,
                allow_origins=list(settings.mini_app_origins),
                allow_methods=["GET", "PATCH"],
                allow_headers=[INIT_DATA_HEADER, "Content-Type"],
            )
    else:
        LOGGER.warning("mini_app_api_disabled", extra={"reason": "MAX_BOT_TOKEN is not set"})
    # Prometheus scrape endpoint of this process (role 1 wires collection; no user data in labels).
    app.mount("/metrics", make_asgi_app())
    app.mount("/", mcp_app)
    return app


async def main() -> None:
    try:
        settings = McpSettings.from_env()
    except ValueError as exc:
        raise SystemExit(f"Неверные настройки MCP: {exc}") from None
    # One engine for the process lifetime; uvicorn stops on SIGINT/SIGTERM after finishing requests.
    async with postgres_engine(PostgresSettings.from_env()) as engine:
        app = build_app(build_tools(session_factory(engine)), settings)
        server = uvicorn.Server(uvicorn.Config(app, host=settings.host, port=settings.port, log_level="info"))
        await server.serve()


if __name__ == "__main__":
    asyncio.run(main())
