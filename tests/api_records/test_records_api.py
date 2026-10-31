import unittest
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI
from sqlalchemy import delete

from app.api.records import ACTION_ID_HEADER, INIT_DATA_HEADER, build_records_router
from app.mcp_server.__main__ import McpSettings, build_app, build_tools
from app.mcp_server.context import TrustedContext
from app.mcp_server.tools import EventTools
from app.planner.event_repository import EventMutationStorageError
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
        app = build_app(EventTools(None, None, None, None), settings)  # type: ignore[arg-type]
        paths = {getattr(route, "path", "") for route in app.routes}
        self.assertFalse(any(path.startswith("/api") for path in paths))

    async def test_cors_allows_authenticated_delete_headers(self) -> None:
        app = build_app(
            EventTools(None, None, None, None),  # type: ignore[arg-type]
            McpSettings(
                service_token="t" * 32,
                bot_token=BOT_TOKEN,
                mini_app_origins=("https://app.example.org",),
            ),
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.options(
                "/api/events/some-id",
                headers={
                    "Origin": "https://app.example.org",
                    "Access-Control-Request-Method": "DELETE",
                    "Access-Control-Request-Headers": "X-Max-Init-Data, X-Action-Id",
                },
            )
        self.assertEqual(response.status_code, 200)
        self.assertIn("DELETE", response.headers["access-control-allow-methods"])
        self.assertIn("x-action-id", response.headers["access-control-allow-headers"].lower())


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
        with patch("app.mcp_server.__main__.clock", return_value=NOW):
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
                    await self.client.delete(f"/api/events/{uuid4()}", headers=headers),
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

    async def test_deleted_record_disappears_from_mini_app_and_cannot_change_status(self) -> None:
        created = await self.create(event_type="task", name="Хакатон", date="2026-10-24")
        context = TrustedContext(
            owner_id=str(self.user_id), timezone="Europe/Moscow", request_id="r", action_id="delete"
        )
        deleted = await self.tools.delete_event(context, {"event_id": created["id"]})
        self.assertIn("deleted", deleted)
        response = await self.client.get("/api/events", params=DAY, headers=self.headers())
        self.assertEqual(response.json()["events"], [])
        response = await self.client.get(
            "/api/events/search",
            params={"query": "хакатон", "timezone": "Europe/Moscow"},
            headers=self.headers(),
        )
        self.assertEqual(response.json()["events"], [])
        response = await self.client.patch(
            f"/api/tasks/{created['id']}/status", json={"status": "done"}, headers=self.headers()
        )
        self.assertEqual(response.status_code, 404)

    async def test_delete_all_types_and_replay_are_persisted(self) -> None:
        inputs = (
            {"event_type": "task", "name": "Удалить задачу", "date": "2026-10-24"},
            {"event_type": "meeting", "name": "Удалить встречу", "datetime": "2026-10-24T10:00:00+03:00"},
            {
                "event_type": "reminder",
                "name": "Удалить напоминание",
                "datetime": "2026-10-24T12:00:00+03:00",
            },
        )
        for arguments in inputs:
            with self.subTest(kind=arguments["event_type"]):
                record = await self.create(**arguments)
                headers = {**self.headers(), ACTION_ID_HEADER: str(uuid4())}
                path = f"/api/events/{record['id']}"
                first = await self.client.delete(path, headers=headers)
                self.assertEqual(first.status_code, 200)
                self.assertEqual(first.json()["deleted"], record)
                repeated = await self.client.delete(path, headers=headers)
                self.assertEqual(repeated.status_code, 200)
                self.assertEqual(repeated.json(), first.json())
                # A genuinely new attempt sees no accessible record, while replay still works.
                new_attempt = await self.client.delete(
                    path, headers={**self.headers(), ACTION_ID_HEADER: str(uuid4())}
                )
                self.assertEqual(new_attempt.status_code, 404)
        listed = await self.client.get("/api/events", params=DAY, headers=self.headers())
        self.assertEqual(listed.json()["events"], [])

    async def test_delete_owner_and_action_conflicts_do_not_change_other_records(self) -> None:
        first = await self.create(event_type="task", name="Первая задача", date="2026-10-24")
        second = await self.create(event_type="task", name="Вторая задача", date="2026-10-24")
        foreign = await self.client.request(
            "DELETE",
            f"/api/events/{first['id']}",
            headers={**self.headers(self.other_id), ACTION_ID_HEADER: str(uuid4())},
            json={"owner_id": str(self.user_id)},
        )
        self.assertEqual(foreign.status_code, 404)
        action = str(uuid4())
        headers = {**self.headers(), ACTION_ID_HEADER: action}
        self.assertEqual(
            (await self.client.delete(f"/api/events/{first['id']}", headers=headers)).status_code, 200
        )
        conflict = await self.client.delete(f"/api/events/{second['id']}", headers=headers)
        self.assertEqual(conflict.status_code, 409)
        self.assertEqual(conflict.json()["error"]["code"], "operation_conflict")
        remaining = await self.client.get("/api/events", params=DAY, headers=self.headers())
        self.assertEqual([event["id"] for event in remaining.json()["events"]], [second["id"]])

    async def test_delete_storage_error_is_not_acknowledged_as_success(self) -> None:
        record = await self.create(event_type="task", name="Сохранить до успешного удаления")
        headers = {**self.headers(), ACTION_ID_HEADER: str(uuid4())}
        path = f"/api/events/{record['id']}"
        with patch.object(
            type(self.tools.editor),
            "delete_event",
            AsyncMock(side_effect=EventMutationStorageError("private infrastructure detail")),
        ):
            response = await self.client.delete(path, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error"]["code"], "temporary_unavailable")
        self.assertNotIn("private infrastructure", response.text)
        retry = await self.client.delete(path, headers=headers)
        self.assertEqual(retry.status_code, 200)

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

    async def test_patch_task_changes_title_period_and_search(self) -> None:
        task = await self.create(event_type="task", name="Старое название", date="2026-10-24")
        headers = {**self.headers(), ACTION_ID_HEADER: str(uuid4())}
        body = {
            "name": " Новый отчёт ",
            "datetime": "2026-10-25T10:30:00+03:00",
            "end_time": "2026-10-25T12:00:00+03:00",
        }
        response = await self.client.patch(
            f"/api/events/{task['id']}", params={"timezone": "Europe/Moscow"}, json=body, headers=headers
        )
        self.assertEqual(response.status_code, 200, response.text)
        changed = response.json()["updated"]
        self.assertEqual(
            (changed["name"], changed["datetime"], changed["end_time"], changed["date"]),
            ("Новый отчёт", "2026-10-25T07:30:00Z", "2026-10-25T09:00:00Z", None),
        )
        self.assertEqual(changed["created_at"], task["created_at"])
        self.assertEqual(
            (await self.client.get("/api/events", params=DAY, headers=self.headers())).json()["events"], []
        )
        found = await self.client.get(
            "/api/events/search",
            params={"query": "отчёт", "timezone": "Europe/Moscow"},
            headers=self.headers(),
        )
        self.assertEqual(found.json()["events"][0]["id"], task["id"])
        replay = await self.client.patch(
            f"/api/events/{task['id']}", params={"timezone": "Europe/Moscow"}, json=body, headers=headers
        )
        self.assertEqual(replay.json(), response.json())
        conflict = await self.client.patch(
            f"/api/events/{task['id']}",
            params={"timezone": "Europe/Moscow"},
            json={"name": "Другое"},
            headers=headers,
        )
        self.assertEqual(conflict.status_code, 409)

    async def test_patch_task_can_remove_time_range_and_schedule_explicitly(self) -> None:
        task = await self.create(
            event_type="task",
            name="Со сроком",
            datetime="2026-10-24T10:00:00+03:00",
            end_time="2026-10-24T11:00:00+03:00",
        )

        async def change(body):
            response = await self.client.patch(
                f"/api/events/{task['id']}",
                params={"timezone": "Europe/Moscow"},
                json=body,
                headers={**self.headers(), ACTION_ID_HEADER: str(uuid4())},
            )
            self.assertEqual(response.status_code, 200, response.text)
            return response.json()["updated"]

        renamed = await change({"name": "Переименована"})
        self.assertEqual((renamed["datetime"], renamed["end_time"]), (task["datetime"], task["end_time"]))
        no_end = await change({"clear_end_time": True})
        self.assertEqual(no_end["datetime"], task["datetime"])
        self.assertIsNone(no_end["end_time"])
        dated = await change({"date": "2026-10-26"})
        self.assertEqual(dated["date"], "2026-10-26")
        self.assertIsNone(dated["datetime"])
        no_due = await change({"clear_schedule": True})
        self.assertTrue(all(no_due[f] is None for f in ["date", "datetime", "end_time"]))

    async def test_patch_rejects_invalid_values_and_owner_changes(self) -> None:
        task = await self.create(event_type="task", name="Исходная")
        bodies = [
            {},
            [],
            {"name": ""},
            {"name": None},
            {"owner_id": str(self.other_id)},
            {"event_type": "meeting"},
            {"datetime": "2026-10-24T10:00:00"},
            {"date": "2026-02-30"},
            {"date": "2026-10-24", "datetime": "2026-10-24T10:00:00Z"},
            {"clear_schedule": "true"},
            {"clear_schedule": True, "date": "2026-10-24"},
            {"clear_end_time": True, "end_time": "2026-10-24T10:00:00Z"},
            {"datetime": "2026-10-24T12:00:00Z", "end_time": "2026-10-24T10:00:00Z"},
        ]
        for body in bodies:
            with self.subTest(body=body):
                response = await self.client.patch(
                    f"/api/events/{task['id']}",
                    params={"timezone": "Europe/Moscow"},
                    json=body,
                    headers={**self.headers(), ACTION_ID_HEADER: str(uuid4())},
                )
                self.assertEqual(response.status_code, 422, response.text)
                self.assertEqual(response.json()["error"]["code"], "validation_error")
        for record_id in (task["id"], str(uuid4()), "bad-id"):
            response = await self.client.patch(
                f"/api/events/{record_id}",
                params={"timezone": "Europe/Moscow"},
                json={"name": "Чужая"},
                headers={**self.headers(self.other_id), ACTION_ID_HEADER: str(uuid4())},
            )
            self.assertEqual(response.status_code, 404)

    async def test_patch_auth_action_id_and_infrastructure_errors(self) -> None:
        task = await self.create(event_type="task", name="Исходная")
        path = f"/api/events/{task['id']}"
        params = {"timezone": "Europe/Moscow"}
        unauthorized = await self.client.patch(path, content="bad json")
        self.assertEqual(unauthorized.status_code, 401)
        for action in (None, "bad-id"):
            headers = self.headers()
            if action is not None:
                headers[ACTION_ID_HEADER] = action
            response = await self.client.patch(path, params=params, json={"name": "Новое"}, headers=headers)
            self.assertEqual(response.status_code, 422)
        headers = {**self.headers(), ACTION_ID_HEADER: str(uuid4())}
        with patch.object(
            type(self.tools.editor),
            "update_event",
            AsyncMock(side_effect=EventMutationStorageError("private database detail")),
        ):
            response = await self.client.patch(path, params=params, json={"name": "Новое"}, headers=headers)
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private database", response.text)
        retry = await self.client.patch(path, params=params, json={"name": "Новое"}, headers=headers)
        self.assertEqual(retry.status_code, 200)

    async def test_patch_other_types_keeps_required_times(self) -> None:
        for kind in ("meeting", "reminder"):
            record = await self.create(event_type=kind, name="Исходная", datetime="2026-10-24T10:00:00+03:00")
            path = f"/api/events/{record['id']}"

            async def change(body, path=path):
                return await self.client.patch(
                    path,
                    params={"timezone": "Europe/Moscow"},
                    json=body,
                    headers={**self.headers(), ACTION_ID_HEADER: str(uuid4())},
                )

            changed = await change({"name": "Новое", "datetime": "2026-10-25T14:00:00+03:00"})
            self.assertEqual(changed.status_code, 200, changed.text)
            self.assertEqual(changed.json()["updated"]["datetime"], "2026-10-25T11:00:00Z")
            self.assertEqual((await change({"clear_schedule": True})).status_code, 422)
            if kind == "reminder":
                self.assertEqual((await change({"clear_end_time": True})).status_code, 422)

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
