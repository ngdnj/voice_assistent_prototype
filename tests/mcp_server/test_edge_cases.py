"""Edge cases of the MCP tools found in review; each defect test guards its fix."""

import asyncio
import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastmcp import Client
from sqlalchemy import delete, func, select

from app.mcp_server.__main__ import build_tools
from app.mcp_server.context import TrustedContext
from app.mcp_server.server import build_mcp_server
from app.mcp_server.tools import EventTools
from app.planner.event_service import EventService
from app.planner.meeting_service import MeetingService
from app.planner.query import RecordQuery
from app.planner.reminder_service import ReminderService
from app.planner.service import TaskService
from app.storage.memory import InMemoryTaskRepository
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.tables import events, operations
from tests.storage_pg import integration_settings, migrate_test_database, requires_postgres

NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)


class McpArgumentEdgeTests(unittest.IsolatedAsyncioTestCase):
    """In-memory MCP client with the real tool schemas."""

    async def asyncSetUp(self) -> None:
        self.repository = InMemoryTaskRepository()
        tasks = TaskService(repository=self.repository, clock=lambda: NOW)
        tools = EventTools(
            tasks,
            AsyncMock(spec=MeetingService),
            AsyncMock(spec=ReminderService),
            AsyncMock(spec=EventService),
        )
        context = TrustedContext(
            owner_id="user-42", timezone="Europe/Moscow", request_id="r", action_id="a:1"
        )
        self.client = Client(build_mcp_server(tools, lambda: context))
        await self.client.__aenter__()
        self.addAsyncCleanup(self.client.__aexit__, None, None, None)

    async def test_owner_arguments_never_change_the_owner(self) -> None:
        # AGENTS §8: user_id is set by the server; a model-supplied owner must not create a record for it.
        arguments = {"event_type": "task", "name": "Чужая", "user_id": "victim", "owner_id": "victim"}
        result = await self.client.call_tool("create_event", arguments, raise_on_error=False)
        self.assertTrue(result.is_error or "error" in (result.structured_content or {}))
        for owner in ("victim", "user-42"):
            self.assertEqual(await self.repository.list(RecordQuery(owner_id=owner)), [])

    async def test_schema_level_argument_errors_are_structured(self) -> None:
        # INTEGRATION §3 "Формат ошибки": errors come in the tool result, not as a transport error.
        # Fact: FastMCP/pydantic rejects them before EventTools: isError with raw pydantic text.
        for name, arguments in (
            ("create_event", {"event_type": "task"}),
            ("create_event", {"event_type": "task", "name": 5}),
            ("get_event_by_query", {"query": "отчёт", "limit": 2.5}),
            (
                "get_events_by_period",
                {
                    "start_time": "2026-10-24T00:00:00Z",
                    "end_time": "2026-10-25T00:00:00Z",
                    "event_types": "task",
                },
            ),
        ):
            with self.subTest(name=name, arguments=arguments):
                result = await self.client.call_tool(name, arguments, raise_on_error=False)
                self.assertFalse(result.is_error)
                self.assertEqual((result.structured_content or {})["error"]["code"], "validation_error")

    async def test_date_must_be_calendar_date(self) -> None:
        # INTEGRATION §3: date is YYYY-MM-DD; fact: ISO week date "2026-W43-1" is stored as 2026-10-19.
        arguments = {"event_type": "task", "name": "Неделя", "date": "2026-W43-1"}
        result = await self.client.call_tool("create_event", arguments, raise_on_error=False)
        self.assertEqual((result.structured_content or {})["error"]["field"], "date")


@requires_postgres
class McpPostgresEdgeTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate_test_database()

    async def asyncSetUp(self) -> None:
        engine_context = postgres_engine(integration_settings())
        engine = await engine_context.__aenter__()
        self.addAsyncCleanup(engine_context.__aexit__, None, None, None)
        self.sessions = session_factory(engine)
        self.owner = f"test-{uuid4()}"
        self.addAsyncCleanup(self.cleanup)
        with patch("app.mcp_server.__main__.clock", return_value=NOW):
            self.tools = build_tools(self.sessions)
        self.actions = 0

    async def cleanup(self) -> None:
        async with self.sessions() as session:
            await session.execute(delete(operations).where(operations.c.owner_id == self.owner))
            await session.execute(delete(events).where(events.c.owner_id == self.owner))
            await session.commit()

    def context(self, timezone: str = "Europe/Moscow", action_id: str | None = None) -> TrustedContext:
        self.actions += 1
        return TrustedContext(
            owner_id=self.owner,
            timezone=timezone,
            request_id="r",
            action_id=action_id or f"job:{self.actions}",
        )

    async def rows(self) -> int:
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count()).select_from(events).where(events.c.owner_id == self.owner)
            )
        return int(count or 0)

    async def test_concurrent_repeats_with_different_data_create_one_record(self) -> None:
        # AGENTS §9: one action_id — one result; incompatible repeats are rejected, even in parallel.
        context = self.context(action_id="race")
        results = await asyncio.gather(
            *(
                self.tools.create_event(context, {"event_type": "task", "name": f"Гонка {i}"})
                for i in range(6)
            )
        )
        self.assertEqual(sum("created" in result for result in results), 1)
        self.assertTrue(
            all(result["error"]["code"] == "operation_conflict" for result in results if "error" in result)
        )
        self.assertEqual(await self.rows(), 1)

    async def test_same_action_with_other_event_type_is_a_conflict(self) -> None:
        # AGENTS §9 / INTEGRATION create_event: the same action_id with other data is operation_conflict.
        # Fact: operations are keyed by (owner, operation_type, action_id), so a retried tool call that
        # switched event_type creates a second record.
        moment = "2026-11-01T10:00:00Z"
        first = await self.tools.create_event(
            self.context(action_id="job:7:0"), {"event_type": "task", "name": "X", "datetime": moment}
        )
        self.assertIn("created", first)
        second = await self.tools.create_event(
            self.context(action_id="job:7:0"), {"event_type": "meeting", "name": "X", "datetime": moment}
        )
        self.assertEqual(second.get("error", {}).get("code"), "operation_conflict")
        self.assertEqual(await self.rows(), 1)

    async def test_nul_character_is_a_validation_error_not_an_outage(self) -> None:
        # INTEGRATION: temporary_unavailable tells the agent to retry; bad data must be validation_error.
        # Fact: PostgreSQL rejects \x00 in text, the driver error is reported as temporary_unavailable.
        created = await self.tools.create_event(self.context(), {"event_type": "task", "name": "a\x00b"})
        self.assertEqual(created["error"]["code"], "validation_error")
        found = await self.tools.get_event_by_query(self.context(), {"query": "a\x00b"})
        self.assertEqual(found["error"]["code"], "validation_error")

    async def test_date_deadline_boundaries_in_offset_zone(self) -> None:
        # Asia/Kathmandu is UTC+05:45: the local midnight of 2026-10-24 is 2026-10-23T18:15:00Z.
        zone = "Asia/Kathmandu"
        await self.tools.create_event(
            self.context(zone), {"event_type": "task", "name": "Срок", "date": "2026-10-24"}
        )
        cases = [
            ("2026-10-23T18:15:00Z", "2026-10-24T18:15:00Z", ["Срок"]),
            ("2026-10-23T18:15:01Z", "2026-10-24T18:15:00Z", []),
            ("2026-10-22T18:15:00Z", "2026-10-23T18:15:00Z", []),
            ("2026-10-22T18:15:00Z", "2026-10-23T18:15:01Z", ["Срок"]),
        ]
        for start, end, expected in cases:
            with self.subTest(start=start, end=end):
                result = await self.tools.get_events_by_period(
                    self.context(zone), {"start_time": start, "end_time": end}
                )
                self.assertEqual([event["name"] for event in result["events"]], expected)

    async def test_date_deadline_sorts_by_local_midnight_among_moments(self) -> None:
        zone = "Europe/Berlin"
        context = self.context(zone)
        await self.tools.create_event(
            self.context(zone), {"event_type": "meeting", "name": "Ночью", "datetime": "2026-10-24T21:30:00Z"}
        )
        await self.tools.create_event(
            self.context(zone), {"event_type": "task", "name": "День", "date": "2026-10-25"}
        )
        await self.tools.create_event(
            self.context(zone), {"event_type": "reminder", "name": "Утро", "datetime": "2026-10-25T05:00:00Z"}
        )
        # 2026-10-25 is the Berlin DST end; local midnight is 2026-10-24T22:00:00Z.
        result = await self.tools.get_events_by_period(
            context, {"start_time": "2026-10-24T00:00:00Z", "end_time": "2026-10-26T00:00:00Z"}
        )
        self.assertEqual([event["name"] for event in result["events"]], ["Ночью", "День", "Утро"])

    async def test_search_without_period_includes_tasks_without_deadline(self) -> None:
        await self.tools.create_event(self.context(), {"event_type": "task", "name": "Отчёт без срока"})
        result = await self.tools.get_event_by_query(self.context(), {"query": "отчёт"})
        self.assertEqual([event["name"] for event in result["events"]], ["Отчёт без срока"])
        period = await self.tools.get_events_by_period(
            self.context(), {"start_time": "2000-01-01T00:00:00Z", "end_time": "2000-12-31T00:00:00Z"}
        )
        self.assertEqual(period, {"events": [], "truncated": False})

    async def test_search_limit_keeps_earliest_by_time(self) -> None:
        # INTEGRATION get_event_by_query: results ordered by time. Fact: without a period tasks are cut by
        # created_at in SQL before sorting by deadline, so limit=1 returns the later deadline.
        await self.tools.create_event(
            self.context(), {"event_type": "task", "name": "Отчёт поздний", "date": "2027-01-01"}
        )
        await self.tools.create_event(
            self.context(), {"event_type": "task", "name": "Отчёт ранний", "date": "2026-11-01"}
        )
        result = await self.tools.get_event_by_query(self.context(), {"query": "отчёт", "limit": 1})
        self.assertEqual([event["name"] for event in result["events"]], ["Отчёт ранний"])

    async def test_zone_without_local_midnight(self) -> None:
        # America/Havana skips 00:00-01:00 on 2026-03-08 (DST start at midnight).
        # Fact: day_bounds_utc raises NonexistentLocalTimeError; the period and even a search without a
        # period answer validation_error for every record of the user.
        zone = "America/Havana"
        await self.tools.create_event(
            self.context(zone), {"event_type": "task", "name": "Гавана", "date": "2026-03-08"}
        )
        period = await self.tools.get_events_by_period(
            self.context(zone), {"start_time": "2026-03-07T05:00:00Z", "end_time": "2026-03-10T04:00:00Z"}
        )
        self.assertEqual([event["name"] for event in period["events"]], ["Гавана"])
        found = await self.tools.get_event_by_query(self.context(zone), {"query": "Гавана"})
        self.assertEqual([event["name"] for event in found["events"]], ["Гавана"])
