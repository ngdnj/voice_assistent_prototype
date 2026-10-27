import argparse
import asyncio
import io
import json
import os
import socket
import unittest
from contextlib import redirect_stdout
from datetime import UTC, datetime
from unittest.mock import patch
from uuid import uuid4

import httpx
import uvicorn
from langchain_core.messages import AIMessage
from pydantic import SecretStr
from sqlalchemy import delete, func, select

from app.agent.__main__ import run
from app.agent.agent import Assistant
from app.agent.bot_backend import AgentBackend
from app.agent.history import RedisChatHistory
from app.agent.mcp import Caller, McpGateway
from app.agent.tools import model_tool_schemas
from app.bot.client import MaxClient
from app.bot.gate import RedisGate
from app.bot.service import BotService
from app.bot.settings import BotSettings
from app.bot.updates import parse_update
from app.config import Settings
from app.llm.model import build_chat_model
from app.mcp_server.__main__ import McpSettings, build_app, build_tools
from app.planner.time import day_bounds_utc
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.tables import events, operations
from app.storage.redis import RedisSettings, redis_connection
from evals.cases import CASES
from evals.run import _run_case
from tests.agent_support import ScriptedModel, tool
from tests.bot.fakes import Collect
from tests.storage_pg import integration_settings, migrate_test_database, requires_postgres

NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)
TIME = {"reference_time": NOW, "timezone": "Europe/Moscow"}
TOKEN = "test-agent-service-token-" + "x" * 32


@requires_postgres
@unittest.skipUnless(os.environ.get("RUN_REDIS_INTEGRATION") == "1", "dedicated Redis required")
class AgentIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """Only the LLM is scripted; MCP HTTP, auth, Redis and PostgreSQL are real."""

    @classmethod
    def setUpClass(cls):
        migrate_test_database()

    async def asyncSetUp(self):
        context = postgres_engine(integration_settings())
        engine = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.sessions = session_factory(engine)
        redis_context = redis_connection(RedisSettings.from_env())
        self.redis = await redis_context.__aenter__()
        self.addAsyncCleanup(redis_context.__aexit__, None, None, None)
        self.owner, self.other = f"agent-test-{uuid4()}", f"agent-test-{uuid4()}"
        self.addAsyncCleanup(self.cleanup)
        self.history = RedisChatHistory(self.redis)
        with patch("app.mcp_server.__main__.clock", return_value=NOW):
            tools = build_tools(self.sessions)
        app = build_app(tools, McpSettings(service_token=TOKEN))
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.setblocking(False)
        self.gateway = McpGateway(f"http://127.0.0.1:{listener.getsockname()[1]}/mcp", TOKEN)
        self.server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        self.serving = asyncio.create_task(self.server.serve(sockets=[listener]))
        self.addAsyncCleanup(self.stop, listener)
        async with asyncio.timeout(10):
            while not self.server.started:  # noqa: ASYNC110
                await asyncio.sleep(0.01)

    async def stop(self, listener):
        self.server.should_exit = True
        await asyncio.wait_for(self.serving, 5)
        listener.close()

    async def cleanup(self):
        owners = (self.owner, self.other)
        async with self.sessions() as session:
            await session.execute(delete(operations).where(operations.c.owner_id.in_(owners)))
            await session.execute(delete(events).where(events.c.owner_id.in_(owners)))
            await session.commit()
        for owner in owners:
            keys = [
                key
                async for key in self.redis.scan_iter(match=f"it-gorky:v1:dialog:{owner.encode().hex()}:*")
            ]
            if keys:
                await self.redis.delete(*keys)

    async def count(self):
        async with self.sessions() as session:
            return await session.scalar(
                select(func.count()).select_from(events).where(events.c.owner_id == self.owner)
            )

    async def create(self, **arguments):
        model = ScriptedModel(responses=[tool(arguments=arguments), AIMessage("Запись создана.")])
        text = "Создай запись" + (" на " + arguments["datetime"] if "datetime" in arguments else "")
        return await Assistant(model, self.gateway, self.history).handle(self.owner, text, **TIME)

    async def test_create_all_types_then_period_search_and_owner_isolation(self):
        task = await self.create(event_type="task", name="Подготовить отчёт", date="2026-10-24")
        await self.create(event_type="meeting", name="Обсудить бюджет", datetime="2026-10-24T10:00:00+03:00")
        await self.create(event_type="reminder", name="Выпить воды", datetime="2026-10-24T12:00:00+03:00")
        self.assertEqual(await self.count(), 3)
        self.assertTrue(task.tool_calls[0].ok)
        period = {"start_time": "2026-10-24T00:00:00+03:00", "end_time": "2026-10-25T00:00:00+03:00"}
        model = ScriptedModel(
            responses=[
                tool("get_events_by_period", period),
                AIMessage("Три записи."),
                tool("get_event_by_query", {"query": "отчёты"}, call_id="search"),
                AIMessage("Одна запись."),
            ]
        )
        agent = Assistant(model, self.gateway, self.history)
        selected = await agent.handle(self.owner, "Что на завтра?", **TIME)
        self.assertEqual(len(selected.tool_calls[0].result["events"]), 3)
        found = await agent.handle(self.owner, "Найди отчёт", **TIME)
        self.assertEqual(
            found.tool_calls[0].result["events"][0]["id"], task.tool_calls[0].result["created"]["id"]
        )
        outsider = await self.gateway.call(
            Caller(self.other, "Europe/Moscow", "r"),
            "get_event_by_query",
            {"query": "отчёт"},
            action_id="read",
        )
        self.assertEqual(outsider["events"], [])

    async def test_clarification_uses_history_without_creating_before_answer(self):
        model = ScriptedModel(
            responses=[
                AIMessage("Во сколько встреча с Иваном?"),
                tool(
                    arguments={
                        "event_type": "meeting",
                        "name": "Встреча с Иваном",
                        "datetime": "2026-10-24T16:00:00+03:00",
                    }
                ),
                AIMessage("Встреча создана на завтра в 16:00."),
            ]
        )
        agent = Assistant(model, self.gateway, self.history)
        first = await agent.handle(self.owner, "Встреча с Иваном завтра", **TIME)
        self.assertEqual(first.tool_calls, [])
        self.assertEqual(await self.count(), 0)
        reply = await agent.handle(self.owner, "В 16", **TIME)
        self.assertTrue(reply.tool_calls[0].ok)
        self.assertEqual(await self.count(), 1)
        self.assertTrue(any(m.content == first.text for m in model.seen[1]))

    async def test_restart_after_commit_replays_same_action_and_completed_turn(self):
        actual = self.gateway

        class LostResponse:
            async def list_tools(self, caller):
                return await actual.list_tools(caller)

            async def call(self, *args, **kwargs):
                await actual.call(*args, **kwargs)
                raise ConnectionError("synthetic lost response after commit")

        operation = str(uuid4())
        with self.assertRaises(ConnectionError):
            await Assistant(ScriptedModel(responses=[tool()]), LostResponse(), self.history).handle(
                self.owner,
                "Отчёт",
                operation_id=operation,
                **TIME,
            )
        self.assertEqual(await self.count(), 1)
        model = ScriptedModel(responses=[AIMessage("Задача создана.")])
        # A new repository instance reads the pending plan from Redis.
        agent = Assistant(model, actual, RedisChatHistory(self.redis))
        reply = await agent.handle(self.owner, "Отчёт", operation_id=operation, **TIME)
        repeated = await agent.handle(self.owner, "Отчёт", operation_id=operation, **TIME)
        self.assertEqual(await self.count(), 1)
        self.assertEqual(repeated.tool_calls[0].result, reply.tool_calls[0].result)
        self.assertEqual(len(model.seen), 1)

    async def test_wrong_service_token_and_server_validation(self):
        caller = Caller(self.owner, "Europe/Moscow", "r")
        wrong = McpGateway(self.gateway.url, "w" * 32)
        denied = await wrong.call(
            caller, "create_event", {"event_type": "task", "name": "x"}, action_id="one"
        )
        self.assertEqual(denied["error"]["code"], "unauthorized")
        invalid = await self.gateway.call(
            caller, "create_event", {"event_type": "meeting", "name": "x"}, action_id="one"
        )
        self.assertEqual(invalid["error"]["code"], "validation_error")
        self.assertEqual(await self.count(), 0)

    async def test_history_ttl_whole_turn_budget_and_reset(self):
        model = ScriptedModel(responses=[AIMessage("Во сколько встреча?")])
        await Assistant(model, self.gateway, self.history).handle(
            self.owner, "Встреча", operation_id="question", **TIME
        )
        key = f"it-gorky:v1:dialog:{self.owner.encode().hex()}:{b'private'.hex()}:history"
        self.assertGreater(await self.redis.ttl(key), 604790)
        tiny = RedisChatHistory(self.redis, max_tokens=1)
        self.assertEqual(await tiny.load(self.owner, "private"), [])
        self.assertEqual(len(await self.history.load(self.owner, "private")), 2)
        await self.history.reset(self.owner, "private")
        self.assertEqual(await self.history.load(self.owner, "private"), [])

    async def test_cli_composes_the_same_backend_and_does_not_log_content(self):
        secret_text = "private-user-content-please-do-not-log"
        model = ScriptedModel(responses=[tool(), AIMessage("Задача создана.")])
        args = argparse.Namespace(
            owner_id=self.owner,
            chat_id="private",
            timezone="Europe/Moscow",
            reference_time=NOW,
            operation_id=uuid4(),
            request_id=uuid4(),
        )
        output = io.StringIO()
        with (
            patch("app.agent.__main__.build_chat_model", return_value=model),
            patch("app.agent.__main__.get_settings", return_value=Settings(mcp_url=self.gateway.url)),
            patch.dict(os.environ, {"MCP_SERVICE_TOKEN": TOKEN}),
            redirect_stdout(output),
            self.assertLogs("it_gorky", level="INFO") as logs,
        ):
            self.assertEqual(await run(args, secret_text), 0)
        self.assertEqual(output.getvalue().strip(), "Задача создана.")
        self.assertNotIn(secret_text, " ".join(logs.output))
        self.assertNotIn(TOKEN, " ".join(logs.output))
        self.assertEqual(await self.count(), 1)

    async def test_gigachat_sdk_accepts_real_mcp_schemas_without_an_api_call(self):
        model = build_chat_model(Settings(gigachat_credentials=SecretStr("test-only-not-an-api-key")))
        schemas = await self.gateway.list_tools(Caller(self.owner, "Europe/Moscow", "schema-check"))
        bound = model.bind_tools(model_tool_schemas(schemas))
        self.assertEqual(
            {tool["function"]["name"] for tool in bound.kwargs["tools"]},
            {
                "create_event",
                "get_events_by_period",
                "get_event_by_query",
                "reschedule_meeting",
                "update_event",
                "delete_event",
            },
        )

    async def test_evaluation_uses_real_postgres_and_restores_past_seed_times(self):
        from zoneinfo import ZoneInfo

        for case_name in ("today_what", "yekb_today_after_local_midnight"):
            with self.subTest(case=case_name):
                case = next(c for c in CASES if c.id == case_name)
                day = case.now.astimezone(ZoneInfo(case.timezone)).date()
                start, end = day_bounds_utc(day, case.timezone)
                model = ScriptedModel(
                    responses=[
                        tool(
                            "get_events_by_period",
                            {"start_time": start.isoformat(), "end_time": end.isoformat()},
                        ),
                        AIMessage("Планы найдены."),
                    ]
                )
                result = await _run_case(case, Settings(enable_delete_event=False), model)
                self.assertTrue(result.passed, result.errors)

    async def test_max_event_to_agent_mcp_postgres_and_reply(self):
        # Real normalized MAX events and MaxClient; only its external HTTP transport
        # and the LLM are scripted. Redis, MCP HTTP and PostgreSQL are real.
        self.owner = str(uuid4().int % 10**12 + 1)
        outgoing = []

        def max_api(request):
            self.assertEqual(request.url.path, "/messages")
            outgoing.append(json.loads(request.content)["text"])
            if request.method == "PUT":
                self.assertTrue(request.url.params["message_id"].startswith("sent-"))
                return httpx.Response(200, json={"success": True})
            self.assertEqual(request.url.params["chat_id"], "777")
            return httpx.Response(200, json={"message": {"body": {"mid": f"sent-{len(outgoing)}"}}})

        model = ScriptedModel(
            responses=[
                tool(arguments={"event_type": "task", "name": "Подготовить демотест", "date": "2026-10-24"}),
                AIMessage("Задача создана на завтра."),
                tool("get_event_by_query", {"query": "демотест"}, call_id="search"),
                AIMessage("Найдена задача подготовить демотест."),
                tool(
                    "get_events_by_period",
                    {"start_time": "2026-10-24T00:00:00+03:00", "end_time": "2026-10-25T00:00:00+03:00"},
                    call_id="period",
                ),
                AIMessage("На завтра одна задача."),
                AIMessage("Во сколько встреча с Иваном?"),
                tool(
                    arguments={
                        "event_type": "meeting",
                        "name": "Встреча с Иваном",
                        "datetime": "2026-10-24T16:00:00+03:00",
                    },
                    call_id="meeting",
                ),
                AIMessage("Встреча создана на завтра в 16:00."),
            ]
        )
        prefix = f"it-gorky:test:max-agent:{uuid4()}"
        gate = RedisGate(self.redis, prefix=prefix)
        collect = Collect()
        settings = BotSettings(token="test-token", test_chat_id="777")
        client = MaxClient(settings, transport=httpx.MockTransport(max_api))
        self.addAsyncCleanup(client.aclose)
        self.addAsyncCleanup(collect.drain)
        bot = BotService(
            sender=client,
            gate=gate,
            backend=AgentBackend(Assistant(model, self.gateway, self.history)),
            submit=collect,
            audio=client,
            recognizer=None,
            timezone="Europe/Moscow",
            processing_timeout_s=10,
            clock=lambda: NOW,
            allowed_chat_id="777",
        )
        requests = [
            "Создай задачу подготовить демотест на завтра",
            "Найди демотест",
            "Что у меня завтра?",
            "Встреча с Иваном завтра",
            "В 16",
        ]
        try:
            for number, text in enumerate(requests):
                raw = {
                    "update_type": "message_created",
                    "message": {
                        "sender": {"user_id": int(self.owner), "is_bot": False},
                        "recipient": {"chat_id": 777},
                        "body": {"mid": f"message-{number}", "text": text},
                    },
                }
                event = parse_update(raw)
                self.assertEqual(await bot.handle(event), "accepted")
                await collect.drain()
                self.assertEqual(await bot.handle(event), "duplicate")
            self.assertEqual(await self.count(), 2)
            self.assertEqual(outgoing.count("Задачи:\n1) Завтра, без времени — Подготовить демотест"), 2)
            self.assertIn("Во сколько встреча с Иваном?", outgoing)
            self.assertEqual(len(model.seen), 9)
            self.assertFalse(await gate.is_busy(self.owner))
            self.assertEqual(len(outgoing), 10)  # Five statuses, each replaced with its answer.
        finally:
            keys = [key async for key in self.redis.scan_iter(match=f"{prefix}:*")]
            if keys:
                await self.redis.delete(*keys)
