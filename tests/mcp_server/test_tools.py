import unittest
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock

from fastmcp import Client

from app.mcp_server.context import TrustedContext, UnauthorizedError
from app.mcp_server.server import build_mcp_server
from app.mcp_server.tools import EventTools
from app.planner.meeting_repository import MeetingStorageError
from app.planner.meeting_service import MeetingService
from app.planner.reminder_service import ReminderService
from app.planner.service import TaskService
from app.storage.memory import InMemoryTaskRepository

NOW = datetime(2026, 10, 23, 8, tzinfo=UTC)
CONTEXT = TrustedContext(
    owner_id="user-42", timezone="Europe/Moscow", request_id="req-1", action_id="job-1:0"
)


class McpToolTests(unittest.IsolatedAsyncioTestCase):
    """In-memory MCP client: real tool schemas and serialization, planner services with test storage."""

    async def asyncSetUp(self) -> None:
        self.tasks = TaskService(repository=InMemoryTaskRepository(), clock=lambda: NOW)
        self.meetings = AsyncMock(spec=MeetingService)
        self.reminders = AsyncMock(spec=ReminderService)
        self.context: TrustedContext = CONTEXT
        mcp = build_mcp_server(EventTools(self.tasks, self.meetings, self.reminders), lambda: self.provide())
        self.client = Client(mcp)
        await self.client.__aenter__()
        self.addAsyncCleanup(self.client.__aexit__, None, None, None)

    def provide(self) -> TrustedContext:
        if self.context is None:
            raise UnauthorizedError("no context")
        return self.context

    async def call(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = await self.client.call_tool(name, arguments, raise_on_error=False)
        self.assertFalse(result.is_error, result)
        content: dict[str, Any] = result.structured_content or {}
        return content

    async def test_schemas_do_not_let_the_model_choose_owner_or_action(self) -> None:
        tools = {tool.name: tool for tool in await self.client.list_tools()}
        self.assertEqual(set(tools), {"create_event", "get_events_by_period", "get_event_by_query"})
        for tool in tools.values():
            properties = set(tool.input_schema["properties"])
            self.assertFalse(properties & {"user_id", "owner_id", "action_id", "request_id"}, tool.name)
            self.assertTrue(tool.description)
        self.assertEqual(
            tools["create_event"].input_schema["properties"]["event_type"]["enum"],
            ["task", "meeting", "reminder"],
        )

    async def test_create_task_and_replay_same_action(self) -> None:
        arguments = {"event_type": "task", "name": " Отчёт ", "date": "2026-10-24"}
        created = (await self.call("create_event", arguments))["created"]
        self.assertEqual(
            {key: created[key] for key in ("event_type", "name", "status", "date", "datetime", "end_time")},
            {
                "event_type": "task",
                "name": "Отчёт",
                "status": "todo",
                "date": "2026-10-24",
                "datetime": None,
                "end_time": None,
            },
        )
        self.assertEqual(created["created_at"], "2026-10-23T08:00:00Z")
        self.assertNotIn("owner_id", created)
        # A retried tool call of the same action returns the same record.
        self.assertEqual((await self.call("create_event", arguments))["created"], created)
        conflict = await self.call("create_event", {**arguments, "name": "Другое"})
        self.assertEqual(conflict["error"]["code"], "operation_conflict")

    async def test_argument_errors_are_structured(self) -> None:
        cases = {
            "unknown type": ({"event_type": "note", "name": "x"}, "event_type"),
            "meeting without time": ({"event_type": "meeting", "name": "Встреча"}, "datetime"),
            "reminder without time": ({"event_type": "reminder", "name": "Позвонить"}, "datetime"),
            "meeting with date": (
                {
                    "event_type": "meeting",
                    "name": "Встреча",
                    "date": "2026-10-24",
                    "datetime": "2026-10-24T10:00:00Z",
                },
                "date",
            ),
            "date and datetime": (
                {
                    "event_type": "task",
                    "name": "Отчёт",
                    "date": "2026-10-24",
                    "datetime": "2026-10-24T10:00:00Z",
                },
                "date",
            ),
            "task with end": (
                {"event_type": "task", "name": "Отчёт", "end_time": "2026-10-24T10:00:00Z"},
                "end_time",
            ),
            "naive time": (
                {"event_type": "reminder", "name": "Позвонить", "datetime": "2026-10-24T10:00:00"},
                "datetime",
            ),
            "bad date": ({"event_type": "task", "name": "Отчёт", "date": "24.10.2026"}, "date"),
            "midnight is not a date": (
                {"event_type": "task", "name": "Отчёт", "date": "2026-10-24T00:00:00"},
                "date",
            ),
            "blank name": ({"event_type": "task", "name": "   "}, "name"),
            "long name": ({"event_type": "task", "name": "я" * 201}, "name"),
            "long description": (
                {"event_type": "task", "name": "Отчёт", "description": "я" * 2001},
                "description",
            ),
        }
        for case, (arguments, field) in cases.items():
            with self.subTest(case=case):
                result = await self.call("create_event", arguments)
                self.assertEqual(result["error"]["code"], "validation_error")
                self.assertEqual(result["error"]["field"], field)
        self.meetings.create_meeting.assert_not_awaited()
        self.reminders.create_reminder.assert_not_awaited()
        self.assertEqual(await self.tasks.list_tasks(_all_tasks()), [])

    async def test_meeting_end_is_optional_and_passed_through(self) -> None:
        self.meetings.create_meeting.side_effect = _echo_meeting
        result = await self.call(
            "create_event",
            {"event_type": "meeting", "name": "Встреча", "datetime": "2026-10-24T10:00:00+03:00"},
        )
        self.assertEqual(result["created"]["datetime"], "2026-10-24T07:00:00Z")
        self.assertIsNone(result["created"]["end_time"])
        kwargs = self.meetings.create_meeting.await_args.kwargs
        self.assertEqual(
            (kwargs["owner_id"], kwargs["operation_id"], kwargs["ends_at"]), ("user-42", "job-1:0", None)
        )

    async def test_storage_failure_is_not_a_confirmation(self) -> None:
        self.meetings.create_meeting.side_effect = MeetingStorageError("down")
        result = await self.call(
            "create_event", {"event_type": "meeting", "name": "Встреча", "datetime": "2026-10-24T10:00:00Z"}
        )
        self.assertEqual(result["error"]["code"], "temporary_unavailable")
        self.assertNotIn("created", result)
        self.meetings.list_meetings.side_effect = MeetingStorageError("down")
        result = await self.call(
            "get_events_by_period",
            {
                "start_time": "2026-10-24T00:00:00Z",
                "end_time": "2026-10-25T00:00:00Z",
                "event_types": ["meeting"],
            },
        )
        self.assertEqual(result["error"]["code"], "temporary_unavailable")

    async def test_missing_context_is_rejected_before_planner(self) -> None:
        self.context = None  # type: ignore[assignment]
        result = await self.call("create_event", {"event_type": "task", "name": "Отчёт"})
        self.assertEqual(result["error"]["code"], "unauthorized")
        self.context = TrustedContext(
            owner_id="user-42", timezone="Europe/Moscow", request_id="r", action_id=None
        )
        result = await self.call("create_event", {"event_type": "task", "name": "Отчёт"})
        self.assertEqual(result["error"]["code"], "unauthorized")
        self.assertEqual(await self.tasks.list_tasks(_all_tasks()), [])

    async def test_period_and_search_arguments(self) -> None:
        self.meetings.list_meetings.return_value = []
        self.reminders.list_reminders.return_value = []
        cases = {
            "reversed": (
                {"start_time": "2026-10-25T00:00:00Z", "end_time": "2026-10-24T00:00:00Z"},
                "end_time",
            ),
            "too long": (
                {"start_time": "2026-01-01T00:00:00Z", "end_time": "2027-01-03T00:00:00Z"},
                "end_time",
            ),
            "naive": (
                {"start_time": "2026-10-24T00:00:00", "end_time": "2026-10-25T00:00:00Z"},
                "start_time",
            ),
            "bad types": (
                {
                    "start_time": "2026-10-24T00:00:00Z",
                    "end_time": "2026-10-25T00:00:00Z",
                    "event_types": ["note"],
                },
                "event_types",
            ),
        }
        for case, (arguments, field) in cases.items():
            with self.subTest(case=case):
                result = await self.call("get_events_by_period", arguments)
                self.assertEqual(
                    (result["error"]["code"], result["error"]["field"]), ("validation_error", field)
                )
        for case, (arguments, field) in {
            "empty query": ({"query": "  "}, "query"),
            "limit too big": ({"query": "отчёт", "limit": 51}, "limit"),
            "limit zero": ({"query": "отчёт", "limit": 0}, "limit"),
        }.items():
            with self.subTest(case=case):
                result = await self.call("get_event_by_query", arguments)
                self.assertEqual(
                    (result["error"]["code"], result["error"]["field"]), ("validation_error", field)
                )
        empty = await self.call(
            "get_events_by_period",
            {
                "start_time": "2026-10-24T00:00:00Z",
                "end_time": "2026-10-25T00:00:00Z",
                "event_types": ["meeting"],
            },
        )
        self.assertEqual(empty, {"events": [], "truncated": False})


def _all_tasks() -> Any:
    from app.planner.query import RecordQuery

    return RecordQuery(owner_id="user-42")


async def _echo_meeting(**kwargs: Any) -> Any:
    from app.planner.meeting import create_meeting

    return create_meeting(
        owner_id=kwargs["owner_id"],
        title=kwargs["title"],
        starts_at=kwargs["starts_at"],
        ends_at=kwargs["ends_at"],
        created_at=NOW,
        description=kwargs["description"],
    )
