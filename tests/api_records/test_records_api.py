import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import delete

from app.api.records import INIT_DATA_HEADER, build_records_router
from app.mcp_server.__main__ import McpSettings, build_app, build_tools
from app.mcp_server.context import TrustedContext
from app.mcp_server.tools import EventTools
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.tables import events, operations
from tests.api_records import BOT_TOKEN, signed_init_data
from tests.storage_pg import integration_settings, migrate_test_database, requires_postgres

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
DAY = {
    "start_time": "2026-10-24T00:00:00+03:00",
    "end_time": "2026-10-25T00:00:00+03:00",
    "timezone": "Europe/Moscow",
}


class DisabledApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_without_bot_token_there_are_no_records_routes(self) -> None:
        settings = McpSettings(service_token="t" * 32, bot_token=None)
        app = build_app(EventTools(None, None, None), settings)  # type: ignore[arg-type]
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertFalse(any(path.startswith("/api") for path in paths))


@requires_postgres
class RecordsApiTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate_test_database()

    async def asyncSetUp(self) -> None:
        engine_context = postgres_engine(integration_settings())
        engine = await engine_context.__aenter__()
        self.addAsyncCleanup(engine_context.__aexit__, None, None, None)
        self.sessions = session_factory(engine)
        # MAX user ids: the owner is str(user.id) from verified initData.
        self.user_id = int(uuid4().int % 10**12) + 1
        self.other_id = self.user_id + 1
        self.addAsyncCleanup(self.cleanup)
        self.tools = build_tools(self.sessions)
        app = FastAPI()
        app.include_router(build_records_router(self.tools, bot_token=BOT_TOKEN, clock=lambda: NOW))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)
        self.actions = 0

    async def cleanup(self) -> None:
        async with self.sessions() as session:
            owners = (str(self.user_id), str(self.other_id))
            await session.execute(delete(operations).where(operations.c.owner_id.in_(owners)))
            await session.execute(delete(events).where(events.c.owner_id.in_(owners)))
            await session.commit()

    def headers(self, user_id: int | None = None, **changes: Any) -> dict[str, str]:
        init_data = signed_init_data(
            user_id=user_id or self.user_id, auth_date=NOW - timedelta(minutes=5), **changes
        )
        return {INIT_DATA_HEADER: init_data}

    async def create(self, **arguments: Any) -> dict[str, Any]:
        """Create through the same tools the bot's agent uses via MCP."""
        self.actions += 1
        context = TrustedContext(
            owner_id=str(self.user_id),
            timezone="Europe/Moscow",
            request_id="r",
            action_id=f"a-{self.actions}",
        )
        result = await self.tools.create_event(context, arguments)
        created: dict[str, Any] = result["created"]
        return created

    async def test_requests_without_valid_init_data_get_401_first(self) -> None:
        for headers in (
            {},
            {INIT_DATA_HEADER: "garbage"},
            self.headers(token="other-bot-token"),
            {INIT_DATA_HEADER: signed_init_data(user_id=self.user_id, auth_date=NOW - timedelta(days=2))},
        ):
            with self.subTest(headers=list(headers)):
                # Missing query parameters must not turn an unauthenticated call into a 422.
                for response in (
                    await self.client.get("/api/events", headers=headers),
                    await self.client.get("/api/events/search", headers=headers),
                    await self.client.patch(f"/api/tasks/{uuid4()}/status", headers=headers),
                ):
                    self.assertEqual(response.status_code, 401)
                    self.assertEqual(response.json()["error"]["code"], "unauthorized")

    async def test_period_tabs_and_search_show_bot_records(self) -> None:
        await self.create(event_type="task", name="Сдать отчёт", date="2026-10-24")
        await self.create(event_type="meeting", name="Встреча с Иваном", datetime="2026-10-24T10:00:00+03:00")
        await self.create(event_type="reminder", name="Выпить воды", datetime="2026-10-24T12:00:00+03:00")
        response = await self.client.get("/api/events", params=DAY, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [event["name"] for event in response.json()["events"]],
            ["Сдать отчёт", "Встреча с Иваном", "Выпить воды"],
        )
        tabs = await self.client.get(
            "/api/events", params={**DAY, "event_types": ["meeting", "reminder"]}, headers=self.headers()
        )
        self.assertEqual([event["event_type"] for event in tabs.json()["events"]], ["meeting", "reminder"])
        found = await self.client.get(
            "/api/events/search",
            params={"query": "встречу", "timezone": "Europe/Moscow"},
            headers=self.headers(),
        )
        self.assertEqual([event["name"] for event in found.json()["events"]], ["Встреча с Иваном"])
        other = await self.client.get("/api/events", params=DAY, headers=self.headers(self.other_id))
        self.assertEqual(other.json(), {"events": [], "truncated": False})

    async def test_status_change_is_persisted(self) -> None:
        task = await self.create(event_type="task", name="Сдать отчёт", date="2026-10-24")
        path = f"/api/tasks/{task['id']}/status"
        response = await self.client.patch(path, json={"status": "in_progress"}, headers=self.headers())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["event"]["status"], "in_progress")
        listed = await self.client.get("/api/events", params=DAY, headers=self.headers())
        self.assertEqual(listed.json()["events"][0]["status"], "in_progress")
        # The same data is visible to the agent: the tools read the same PostgreSQL rows.
        context = TrustedContext(
            owner_id=str(self.user_id), timezone="Europe/Moscow", request_id="r", action_id=None
        )
        period = await self.tools.get_events_by_period(context, DAY)
        self.assertEqual(period["events"][0]["status"], "in_progress")

    async def test_status_change_errors(self) -> None:
        task = await self.create(event_type="task", name="Сдать отчёт")
        meeting = await self.create(
            event_type="meeting", name="Встреча", datetime="2026-10-24T10:00:00+03:00"
        )
        cases = [
            (f"/api/tasks/{task['id']}/status", {"status": "archived"}, self.headers(), 422),
            (f"/api/tasks/{task['id']}/status", None, self.headers(), 422),
            (f"/api/tasks/{task['id']}/status", {"status": "done"}, self.headers(self.other_id), 404),
            (f"/api/tasks/{meeting['id']}/status", {"status": "done"}, self.headers(), 404),
            (f"/api/tasks/{uuid4()}/status", {"status": "done"}, self.headers(), 404),
            ("/api/tasks/not-a-uuid/status", {"status": "done"}, self.headers(), 404),
        ]
        for path, body, headers, status in cases:
            with self.subTest(path=path, body=body, status=status):
                response = await self.client.patch(path, json=body, headers=headers)
                self.assertEqual(response.status_code, status)
                self.assertIn("error", response.json())
        unchanged = await self.client.get(
            "/api/events/search",
            params={"query": "отчёт", "timezone": "Europe/Moscow"},
            headers=self.headers(),
        )
        self.assertEqual(unchanged.json()["events"][0]["status"], "todo")

    async def test_parameter_errors_are_structured(self) -> None:
        cases = [
            ("/api/events", {**DAY, "timezone": "Mars/Olympus"}, "timezone"),
            ("/api/events", {"timezone": "Europe/Moscow"}, "start_time"),
            ("/api/events", {**DAY, "end_time": "2026-10-23T00:00:00+03:00"}, "end_time"),
            ("/api/events", {**DAY, "event_types": ["note"]}, "event_types"),
            ("/api/events/search", {"timezone": "Europe/Moscow"}, "query"),
            ("/api/events/search", {"query": "отчёт", "limit": "100", "timezone": "Europe/Moscow"}, "limit"),
            ("/api/events/search", {"query": "отчёт", "limit": "abc", "timezone": "Europe/Moscow"}, "limit"),
        ]
        for path, params, field in cases:
            with self.subTest(path=path, field=field):
                response = await self.client.get(path, params=params, headers=self.headers())
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["error"]["field"], field)
