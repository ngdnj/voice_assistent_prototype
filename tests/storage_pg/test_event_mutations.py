import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from app.planner.event_repository import EventMutationStorageError
from app.planner.event_service import EventService
from app.planner.meeting import Meeting
from app.planner.meeting_service import MeetingService
from app.planner.operation import CreationOperation, OperationConflictError
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

NOW = datetime(2026, 10, 26, 9, tzinfo=UTC)


@requires_postgres
class EventMutationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        migrate_test_database()

    async def asyncSetUp(self):
        context = postgres_engine(integration_settings())
        engine = await context.__aenter__()
        self.addAsyncCleanup(context.__aexit__, None, None, None)
        self.sessions = session_factory(engine)
        self.owner, self.other = "test-" + uuid4().hex, "test-" + uuid4().hex
        self.addAsyncCleanup(self.cleanup)
        self.repo = PostgresEventMutations(self.sessions)
        self.service = EventService(repository=self.repo, clock=lambda: NOW)
        self.tasks_repo = PostgresTaskRepository(self.sessions)
        self.meetings_repo = PostgresMeetingRepository(self.sessions)
        self.reminders_repo = PostgresReminderRepository(self.sessions)
        self.tasks = TaskService(repository=self.tasks_repo, clock=lambda: NOW)
        self.meetings = MeetingService(repository=self.meetings_repo, clock=lambda: NOW)
        self.reminders = ReminderService(repository=self.reminders_repo, clock=lambda: NOW)

    async def cleanup(self):
        async with self.sessions() as session:
            await session.execute(
                delete(operations).where(operations.c.owner_id.in_([self.owner, self.other]))
            )
            await session.execute(delete(events).where(events.c.owner_id.in_([self.owner, self.other])))
            await session.commit()

    async def seed(self):
        return [
            await self.tasks.create_task(owner_id=self.owner, title="Отчёт", operation_id="t"),
            await self.meetings.create_meeting(
                owner_id=self.owner,
                title="Хакатон",
                operation_id="m",
                starts_at=NOW + timedelta(days=1),
                ends_at=NOW + timedelta(days=1, hours=2),
            ),
            await self.reminders.create_reminder(
                owner_id=self.owner, text="Аптека", operation_id="r", remind_at=NOW + timedelta(days=1)
            ),
        ]

    async def test_all_types_can_be_renamed_and_reindexed_without_changing_identity(self):
        for number, record in enumerate(await self.seed()):
            changed = await self.service.update_event(
                event_id=record.id, owner_id=self.owner, operation_id=f"up-{number}", name=" Новое название "
            )
            self.assertEqual(
                (changed.id, changed.owner_id, changed.created_at),
                (record.id, record.owner_id, record.created_at),
            )
            self.assertEqual(await self.repo.get_owned(event_id=record.id, owner_id=self.owner), changed)
        query = RecordQuery(owner_id=self.owner, query="новое")
        for repository in (self.tasks_repo, self.meetings_repo, self.reminders_repo):
            self.assertEqual(len(await repository.list(query)), 1)
        self.assertEqual(await self.meetings_repo.list(RecordQuery(owner_id=self.owner, query="хакатон")), [])

    async def test_time_changes_follow_each_type_and_move_the_period(self):
        task, meeting, reminder = await self.seed()
        moved = await self.service.update_event(
            event_id=meeting.id,
            owner_id=self.owner,
            operation_id="move",
            name="Созвон",
            moment=NOW + timedelta(days=2),
        )
        self.assertIsInstance(moved, Meeting)
        self.assertEqual(moved.ends_at, moved.starts_at + timedelta(hours=2))
        self.assertEqual(
            await self.meetings_repo.list(
                RecordQuery(
                    owner_id=self.owner, start_time=NOW + timedelta(days=1), end_time=NOW + timedelta(days=2)
                )
            ),
            [],
        )
        updated = await self.service.update_event(
            event_id=reminder.id, owner_id=self.owner, operation_id="remind", moment=NOW + timedelta(days=3)
        )
        self.assertEqual(updated.remind_at, NOW + timedelta(days=3))
        dated = await self.service.update_event(
            event_id=task.id, owner_id=self.owner, operation_id="due", day=date(2026, 10, 30)
        )
        self.assertEqual(dated.due_date, date(2026, 10, 30))
        self.assertIsNone(dated.due_at)

    async def test_deletion_hides_all_reads_and_survives_a_new_repository(self):
        originals = await self.seed()
        for number, record in enumerate(originals):
            deleted = await self.service.delete_event(
                event_id=record.id, owner_id=self.owner, operation_id=f"del-{number}"
            )
            self.assertEqual(deleted, record)
            self.assertIsNone(await self.repo.get_owned(event_id=record.id, owner_id=self.owner))
            restarted = EventService(
                repository=PostgresEventMutations(self.sessions), clock=lambda: NOW + timedelta(days=7)
            )
            self.assertEqual(
                await restarted.delete_event(
                    event_id=record.id, owner_id=self.owner, operation_id=f"del-{number}"
                ),
                record,
            )
            self.assertIsNone(
                await self.service.delete_event(
                    event_id=record.id, owner_id=self.other, operation_id=f"del-{number}"
                )
            )
        for repository in (self.tasks_repo, self.meetings_repo, self.reminders_repo):
            self.assertEqual(await repository.list(RecordQuery(owner_id=self.owner)), [])
        self.assertIsNone(await self.tasks.get_task(task_id=originals[0].id, owner_id=self.owner))
        self.assertIsNone(await self.meetings.get_meeting(meeting_id=originals[1].id, owner_id=self.owner))
        self.assertIsNone(await self.reminders.get_reminder(reminder_id=originals[2].id, owner_id=self.owner))
        self.assertIsNone(
            await self.tasks.update_status(task_id=originals[0].id, owner_id=self.owner, status="done")
        )
        with self.assertRaises(OperationConflictError):
            await self.tasks.create_task(owner_id=self.owner, title="Отчёт", operation_id="t")
        with self.assertRaises(OperationConflictError):
            await self.meetings.create_meeting(
                owner_id=self.owner,
                title="Хакатон",
                operation_id="m",
                starts_at=NOW + timedelta(days=1),
                ends_at=NOW + timedelta(days=1, hours=2),
            )

    async def test_concurrent_retries_and_conflicts_cannot_repeat_or_overwrite_an_action(self):
        task = (await self.seed())[0]
        args = dict(event_id=task.id, owner_id=self.owner, operation_id="up", name="Новое")
        copies = await asyncio.gather(*(self.service.update_event(**args) for _ in range(6)))
        self.assertTrue(all(value == copies[0] for value in copies))
        with self.assertRaises(OperationConflictError):
            await self.service.update_event(**{**args, "name": "Другое"})
        changed = await self.service.update_event(**{**args, "name": "Позднее", "operation_id": "later"})
        self.assertEqual(await self.service.update_event(**args), changed)
        with self.assertRaises(OperationConflictError):
            await self.service.delete_event(event_id=task.id, owner_id=self.owner, operation_id="up")
        self.assertIsNone(await self.service.update_event(**{**args, "owner_id": self.other}))
        deleted = await asyncio.gather(
            *(
                self.service.delete_event(event_id=task.id, owner_id=self.owner, operation_id="delete")
                for _ in range(6)
            )
        )
        self.assertTrue(all(record == changed for record in deleted))
        with self.assertRaises(OperationConflictError):
            await self.service.update_event(**args)

    async def test_stale_snapshot_cannot_overwrite_a_newer_change(self):
        task = (await self.seed())[0]
        changed = await self.service.update_event(
            event_id=task.id, owner_id=self.owner, operation_id="new", name="Новое"
        )
        with self.assertRaises(OperationConflictError):
            await self.repo.update(
                replace(task, title="Устаревшее"),
                previous=task,
                operation=CreationOperation.from_data("stale", event_id=str(task.id)),
                changed_at=NOW,
            )
        self.assertEqual(await self.repo.get_owned(event_id=task.id, owner_id=self.owner), changed)

    async def test_failure_between_operation_insert_and_record_update_rolls_back_both(self):
        task = (await self.seed())[0]
        execute = AsyncSession.execute

        async def fail_update(session, statement, *args, **kwargs):
            if isinstance(statement, Update) and statement.table.name == "events":
                raise SQLAlchemyError("private connection details")
            return await execute(session, statement, *args, **kwargs)

        for method, changes in [
            (self.service.update_event, {"name": "Новое"}),
            (self.service.delete_event, {}),
        ]:
            operation_id = uuid4().hex
            with (
                patch.object(AsyncSession, "execute", fail_update),
                self.assertRaises(EventMutationStorageError) as caught,
            ):
                await method(event_id=task.id, owner_id=self.owner, operation_id=operation_id, **changes)
            self.assertNotIn("private", str(caught.exception))
            self.assertEqual(await self.repo.get_owned(event_id=task.id, owner_id=self.owner), task)
            async with self.sessions() as session:
                self.assertEqual(
                    await session.scalar(
                        select(func.count())
                        .select_from(operations)
                        .where(operations.c.owner_id == self.owner, operations.c.operation_id == operation_id)
                    ),
                    0,
                )
