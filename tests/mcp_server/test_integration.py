import asyncio
import socket
import unittest
from typing import Any
from uuid import uuid4

import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from sqlalchemy import delete, func, select

from app.mcp_server.__main__ import build_tools
from app.mcp_server.context import TrustedContext
from app.mcp_server.server import build_mcp_server, header_context
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.tables import events, operations
from tests.storage_pg import integration_settings, migrate_test_database, requires_postgres

TOKEN = "integration-test-token-" + "x" * 16
DAY = {"start_time": "2026-10-24T00:00:00+03:00", "end_time": "2026-10-25T00:00:00+03:00"}


@requires_postgres
class McpPostgresIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate_test_database()

    async def asyncSetUp(self) -> None:
        engine_context = postgres_engine(integration_settings())
        engine = await engine_context.__aenter__()
        self.addAsyncCleanup(engine_context.__aexit__, None, None, None)
        self.sessions = session_factory(engine)
        self.owner = f"test-{uuid4()}"
        self.other = f"test-{uuid4()}"
        self.addAsyncCleanup(self.cleanup)
        self.action = 0
        self.caller = self.owner
        self.mcp = build_mcp_server(build_tools(self.sessions), self.context)
        self.client = Client(self.mcp)
        await self.client.__aenter__()
        self.addAsyncCleanup(self.client.__aexit__, None, None, None)

    def context(self) -> TrustedContext:
        return TrustedContext(
            owner_id=self.caller, timezone="Europe/Moscow", request_id="req", action_id=f"job:{self.action}"
        )

    async def cleanup(self) -> None:
        async with self.sessions() as session:
            owners = (self.owner, self.other)
            await session.execute(delete(operations).where(operations.c.owner_id.in_(owners)))
            await session.execute(delete(events).where(events.c.owner_id.in_(owners)))
            await session.commit()

    async def call(self, name: str, arguments: dict[str, Any], *, new_action: bool = True) -> dict[str, Any]:
        if new_action:
            self.action += 1
        result = await self.client.call_tool(name, arguments, raise_on_error=False)
        self.assertFalse(result.is_error, result)
        content: dict[str, Any] = result.structured_content or {}
        return content

    async def test_create_period_and_search_flow(self) -> None:
        for arguments in (
            {"event_type": "task", "name": "Сдать отчёт", "date": "2026-10-24"},
            {"event_type": "task", "name": "Позвонить в банк", "datetime": "2026-10-24T15:00:00+03:00"},
            {"event_type": "task", "name": "Когда-нибудь прочитать книгу"},
            {
                "event_type": "meeting",
                "name": "Встреча с Иваном",
                "description": "Обсудить бюджет",
                "datetime": "2026-10-24T10:00:00+03:00",
                "end_time": "2026-10-24T11:00:00+03:00",
            },
            {"event_type": "reminder", "name": "Выпить воды", "datetime": "2026-10-24T12:00:00+03:00"},
            {"event_type": "meeting", "name": "Встреча завтра", "datetime": "2026-10-25T10:00:00+03:00"},
        ):
            self.assertIn("created", await self.call("create_event", arguments), arguments)
        day = await self.call("get_events_by_period", DAY)
        self.assertEqual(
            [event["name"] for event in day["events"]],
            ["Сдать отчёт", "Встреча с Иваном", "Выпить воды", "Позвонить в банк"],
        )
        self.assertFalse(day["truncated"])
        meetings = await self.call("get_events_by_period", {**DAY, "event_types": ["meeting"]})
        self.assertEqual([event["name"] for event in meetings["events"]], ["Встреча с Иваном"])
        found = await self.call("get_event_by_query", {"query": "бюджета"})
        self.assertEqual([event["name"] for event in found["events"]], ["Встреча с Иваном"])
        self.assertEqual(found["events"][0]["end_time"], "2026-10-24T08:00:00Z")
        # Search without a period also finds tasks that have no deadline.
        books = await self.call("get_event_by_query", {"query": "книги", "event_types": ["task"]})
        self.assertEqual([event["name"] for event in books["events"]], ["Когда-нибудь прочитать книгу"])
        limited = await self.call("get_event_by_query", {"query": "встреча", "limit": 1})
        self.assertEqual(len(limited["events"]), 1)
        self.assertEqual(await self.call("get_event_by_query", {"query": "самолёт"}), {"events": []})

    async def test_other_owner_sees_nothing(self) -> None:
        await self.call(
            "create_event", {"event_type": "meeting", "name": "Встреча", "datetime": "2026-10-24T10:00:00Z"}
        )
        self.caller = self.other
        self.assertEqual((await self.call("get_events_by_period", DAY))["events"], [])
        self.assertEqual((await self.call("get_event_by_query", {"query": "встреча"}))["events"], [])

    async def test_retried_tool_call_creates_one_record(self) -> None:
        arguments = {"event_type": "reminder", "name": "Позвонить", "datetime": "2026-10-24T12:00:00+03:00"}
        first = await self.call("create_event", arguments)
        repeats = await asyncio.gather(
            *(self.call("create_event", arguments, new_action=False) for _ in range(5))
        )
        self.assertTrue(all(repeat == first for repeat in repeats))
        async with self.sessions() as session:
            count = await session.scalar(
                select(func.count()).select_from(events).where(events.c.owner_id == self.owner)
            )
        self.assertEqual(count, 1)

    async def test_http_transport_checks_token_and_headers(self) -> None:
        mcp = build_mcp_server(build_tools(self.sessions), header_context(TOKEN))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(mcp.http_app(path="/mcp"), log_level="warning", lifespan="on"))
        serving = asyncio.create_task(server.serve(sockets=[listener]))
        self.addAsyncCleanup(self.stop, server, serving, listener)
        # uvicorn exposes only a `started` flag, so poll it with a hard time limit.
        async with asyncio.timeout(10):
            while not server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.05)
        headers = {"X-Owner-Id": self.owner, "X-Timezone": "Europe/Moscow", "X-Action-Id": "http-1"}
        arguments = {"event_type": "task", "name": "Через HTTP"}

        async def call(authorization: str) -> dict[str, Any]:
            transport = StreamableHttpTransport(
                f"http://127.0.0.1:{port}/mcp", headers={**headers, "Authorization": authorization}
            )
            async with Client(transport) as client:
                result = await client.call_tool("create_event", arguments, raise_on_error=False)
            content: dict[str, Any] = result.structured_content or {}
            return content

        self.assertEqual((await call("Bearer wrong-token"))["error"]["code"], "unauthorized")
        created = await call(f"Bearer {TOKEN}")
        self.assertEqual(created["created"]["name"], "Через HTTP")
        self.assertEqual(await call(f"Bearer {TOKEN}"), created)

    @staticmethod
    async def stop(server: uvicorn.Server, serving: "asyncio.Task[None]", listener: socket.socket) -> None:
        server.should_exit = True
        await serving
        listener.close()
