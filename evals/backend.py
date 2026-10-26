"""Evaluation against the real MCP implementation and a dedicated test PostgreSQL.

No SQLite imitation: each case has its own owner and reference clock. Network
authentication is covered separately by the agent HTTP integration tests.
"""

import os
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from fastmcp import Client
from sqlalchemy import delete

from app.agent.mcp import Caller
from app.mcp_server.context import TrustedContext
from app.mcp_server.server import build_mcp_server
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
from app.storage.postgres.tables import events, operations
from app.storage.postgres.tasks import PostgresTaskRepository


@dataclass
class ScenarioClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


class EvaluationGateway:
    def __init__(self, tools: EventTools, owner: str, timezone: str, clock: ScenarioClock) -> None:
        self.owner = owner
        self.clock = clock
        self.context = TrustedContext(owner, timezone, str(uuid4()), None)
        self.client = Client(build_mcp_server(tools, lambda: self.context))

    async def list_tools(self, caller: Caller) -> list[dict[str, Any]]:
        return [
            {"name": t.name, "description": t.description or "", "parameters": t.input_schema}
            for t in await self.client.list_tools()
        ]

    async def call(
        self, caller: Caller, name: str, arguments: dict[str, Any], *, action_id: str
    ) -> dict[str, Any]:
        if caller.owner_id != self.owner:
            raise ValueError("Evaluation owner mismatch")
        self.context = TrustedContext(caller.owner_id, caller.timezone, caller.request_id, action_id)
        result = await self.client.call_tool(name, arguments, raise_on_error=False)
        if result.is_error or result.structured_content is None:
            raise RuntimeError("Invalid evaluation tool response")
        return result.structured_content

    async def seed(self, arguments: dict[str, Any], *, action_id: str) -> None:
        # Historical reminders were created before they became past. Restore that
        # creation clock for fixtures only; user actions always use the case time.
        original = self.clock.now
        if arguments.get("datetime") is not None:
            self.clock.now = min(original, datetime.fromisoformat(arguments["datetime"]))
        try:
            result = await self.call(
                Caller(self.owner, self.context.timezone, "eval-seed"),
                "create_event",
                arguments,
                action_id=action_id,
            )
            if "error" in result:
                raise ValueError("Invalid evaluation seed")
        finally:
            self.clock.now = original


@asynccontextmanager
async def evaluation_backend(now: datetime, timezone: str) -> AsyncIterator[EvaluationGateway]:
    database = os.environ.get("POSTGRES_TEST_DB", "")
    if not re.fullmatch(r"(?:\w+_)?test(?:_\w+)?", database):
        raise ValueError("Evals require a migrated POSTGRES_TEST_DB with 'test' in its name")
    settings = replace(PostgresSettings.from_env(), database=database)
    owner = f"eval-{uuid4()}"
    async with postgres_engine(settings) as engine:
        sessions = session_factory(engine)
        clock = ScenarioClock(now)
        tools = EventTools(
            TaskService(repository=PostgresTaskRepository(sessions), clock=clock),
            MeetingService(repository=PostgresMeetingRepository(sessions), clock=clock),
            ReminderService(repository=PostgresReminderRepository(sessions), clock=clock),
            editor=EventService(repository=PostgresEventMutations(sessions), clock=clock),
        )
        gateway = EvaluationGateway(tools, owner, timezone, clock)
        try:
            async with gateway.client:
                yield gateway
        finally:
            async with sessions() as session:
                await session.execute(delete(operations).where(operations.c.owner_id == owner))
                await session.execute(delete(events).where(events.c.owner_id == owner))
                await session.commit()
