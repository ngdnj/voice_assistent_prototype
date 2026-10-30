import unittest
from datetime import UTC, date, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import delete

from app.mcp_server.context import TrustedContext
from app.mcp_server.schemas import EventDTO
from app.mcp_server.tools import EventTools
from app.planner.event_service import EventService
from app.planner.meeting_service import MeetingService
from app.planner.query import RecordQuery
from app.planner.reminder_service import ReminderService
from app.planner.service import TaskService
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.event_mutations import PostgresEventMutations
from app.storage.postgres.meetings import PostgresMeetingRepository
from app.storage.postgres.reminders import PostgresReminderRepository
from app.storage.postgres.tables import events, operations
from app.storage.postgres.tasks import PostgresTaskRepository
from tests.storage_pg import integration_settings, migrate_test_database, requires_postgres

NOW = datetime(2026, 10, 30, 9, tzinfo=UTC)


@requires_postgres
class TimeRangeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        migrate_test_database()

    async def asyncSetUp(self):
        context = postgres_engine(integration_settings())
        engine = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.sessions = session_factory(engine)
        self.owner = "test-range-" + uuid4().hex
        self.addAsyncCleanup(self.cleanup)
        self.tasks = TaskService(repository=PostgresTaskRepository(self.sessions), clock=lambda: NOW)
        self.meetings = MeetingService(repository=PostgresMeetingRepository(self.sessions), clock=lambda: NOW)
        self.editor = EventService(repository=PostgresEventMutations(self.sessions), clock=lambda: NOW)
        self.tools = EventTools(
            self.tasks,
            self.meetings,
            ReminderService(repository=PostgresReminderRepository(self.sessions), clock=lambda: NOW),
            self.editor,
        )

    async def cleanup(self):
        async with self.sessions() as session:
            await session.execute(delete(operations).where(operations.c.owner_id == self.owner))
            await session.execute(delete(events).where(events.c.owner_id == self.owner))
            await session.commit()

    async def test_task_range_round_trip_and_mcp_dto(self):
        caller = TrustedContext(self.owner, "Europe/Moscow", "request", "create-range")
        result = await self.tools.create_event(
            caller,
            {
                "event_type": "task",
                "name": "Работа",
                "datetime": "2026-10-30T15:00:00+03:00",
                "end_time": "2026-10-30T17:00:00+03:00",
            },
        )
        self.assertIn("created", result)
        self.assertEqual(result["created"]["end_time"], "2026-10-30T14:00:00Z")
        task = await PostgresTaskRepository(self.sessions).get(
            task_id=UUID(result["created"]["id"]), owner_id=self.owner
        )
        self.assertEqual(task.ends_at, NOW + timedelta(hours=5))
        self.assertEqual(EventDTO.from_record(task).model_dump(mode="json"), result["created"])
        self.assertEqual(
            await self.tools.create_event(
                caller,
                {
                    "event_type": "task",
                    "name": "Работа",
                    "datetime": "2026-10-30T15:00:00+03:00",
                    "end_time": "2026-10-30T17:00:00+03:00",
                },
            ),
            result,
        )
        async with postgres_engine(integration_settings()) as fresh_engine:
            restored = await PostgresTaskRepository(session_factory(fresh_engine)).get(
                task_id=task.id, owner_id=self.owner
            )
            self.assertEqual(restored, task)

    async def test_invalid_ranges_are_structured_errors_without_records(self):
        caller = TrustedContext(self.owner, "Europe/Moscow", "request", "invalid-range")
        for values in (
            {"event_type": "task", "end_time": "2026-10-30T17:00:00+03:00"},
            {"event_type": "task", "date": "2026-10-30", "end_time": "2026-10-30T17:00:00+03:00"},
            {
                "event_type": "task",
                "datetime": "2026-10-30T17:00:00+03:00",
                "end_time": "2026-10-30T15:00:00+03:00",
            },
            {
                "event_type": "meeting",
                "datetime": "2026-10-30T17:00:00+03:00",
                "end_time": "2026-10-30T17:00:00+03:00",
            },
            {
                "event_type": "reminder",
                "datetime": "2026-10-30T17:00:00+03:00",
                "end_time": "2026-10-30T18:00:00+03:00",
            },
            {
                "event_type": "task",
                "datetime": "2026-10-30T17:00:00+03:00",
                "end_time": "2026-10-30T18:00:00",
            },
        ):
            with self.subTest(values=values):
                result = await self.tools.create_event(caller, {"name": "Работа", **values})
                self.assertEqual(result["error"]["code"], "validation_error")
        self.assertEqual(await self.tasks.list_tasks(RecordQuery(owner_id=self.owner)), [])
        self.assertEqual(await self.meetings.list_meetings(RecordQuery(owner_id=self.owner)), [])

    async def test_task_range_move_preserves_duration_and_date_only_clears_end(self):
        task = await self.tasks.create_task(
            owner_id=self.owner,
            title="Работа",
            operation_id="create",
            due_at=NOW,
            ends_at=NOW + timedelta(hours=2),
        )
        moved = await self.editor.update_event(
            event_id=task.id, owner_id=self.owner, operation_id="move", moment=NOW + timedelta(days=1)
        )
        self.assertEqual(moved.ends_at, NOW + timedelta(days=1, hours=2))
        dated = await self.editor.update_event(
            event_id=task.id, owner_id=self.owner, operation_id="date", day=date(2026, 11, 2)
        )
        self.assertEqual(dated.due_date, date(2026, 11, 2))
        self.assertIsNone(dated.due_at)
        self.assertIsNone(dated.ends_at)

    async def test_midnight_ranges_overlap_each_day_with_exclusive_end(self):
        start = datetime(2026, 10, 30, 23, tzinfo=UTC)
        end = start + timedelta(hours=2)
        task = await self.tasks.create_task(
            owner_id=self.owner, title="Работа", operation_id="task", due_at=start, ends_at=end
        )
        meeting = await self.meetings.create_meeting(
            owner_id=self.owner, title="Созвон", operation_id="meeting", starts_at=start, ends_at=end
        )
        next_day = datetime(2026, 10, 31, tzinfo=UTC)
        for service, record, is_task in ((self.tasks, task, True), (self.meetings, meeting, False)):
            query = RecordQuery(
                owner_id=self.owner,
                start_time=next_day,
                end_time=next_day + timedelta(days=1),
                task_period="due" if is_task else "created_at",
                timezone="UTC",
            )
            listed = await (service.list_tasks(query) if is_task else service.list_meetings(query))
            self.assertEqual(listed, [record])
            excluded = RecordQuery(
                owner_id=self.owner,
                start_time=end,
                end_time=end + timedelta(hours=1),
                task_period="due" if is_task else "created_at",
                timezone="UTC",
            )
            self.assertEqual(
                await (service.list_tasks(excluded) if is_task else service.list_meetings(excluded)), []
            )
