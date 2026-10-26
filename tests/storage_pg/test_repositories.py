import asyncio
import unittest
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.planner.meeting import create_meeting
from app.planner.meeting_repository import MeetingAlreadyExistsError
from app.planner.meeting_service import MeetingService
from app.planner.operation import CreationOperation, OperationConflictError
from app.planner.query import RecordQuery
from app.planner.reminder_repository import ReminderStorageError
from app.planner.reminder_service import ReminderService
from app.planner.repository import TaskAlreadyExistsError, TaskStorageError
from app.planner.service import TaskService
from app.planner.task import create_task
from app.planner.time import day_bounds_utc
from app.storage.postgres.engine import postgres_engine, session_factory
from app.storage.postgres.meetings import PostgresMeetingRepository
from app.storage.postgres.reminders import PostgresReminderRepository
from app.storage.postgres.settings import PostgresSettings
from app.storage.postgres.tables import events, operations
from app.storage.postgres.tasks import PostgresTaskRepository
from tests.storage_pg import integration_settings, migrate_test_database, requires_postgres

ROOT = Path(__file__).resolve().parents[2]
CREATED = datetime(2026, 10, 23, 8, 0, 0, 123456, tzinfo=UTC)


class UnavailableDatabaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_database_is_a_storage_error(self) -> None:
        # No confirmation without a database: every operation raises the planner's storage error.
        settings = PostgresSettings(port=1, password="unused")
        engine = create_async_engine(settings.url(), connect_args={"timeout": 1.0})
        self.addAsyncCleanup(engine.dispose)
        sessions = session_factory(engine)
        tasks = TaskService(repository=PostgresTaskRepository(sessions), clock=lambda: CREATED)
        with self.assertRaises(TaskStorageError):
            await tasks.create_task(owner_id="user-42", title="Отчёт", operation_id="op-1")
        with self.assertRaises(TaskStorageError):
            await PostgresTaskRepository(sessions).update_status(task_id=uuid4(), owner_id="u", status="done")
        with self.assertRaises(ReminderStorageError):
            await PostgresReminderRepository(sessions).list(RecordQuery(owner_id="user-42"))


@requires_postgres
class PostgresRepositoryTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls) -> None:
        migrate_test_database()

    async def asyncSetUp(self) -> None:
        self.engine_context = postgres_engine(integration_settings())
        self.engine: AsyncEngine = await self.engine_context.__aenter__()
        self.addAsyncCleanup(self.engine_context.__aexit__, None, None, None)
        self.sessions: async_sessionmaker[AsyncSession] = session_factory(self.engine)
        # Unique owners keep tests independent; cleanup removes only their rows.
        self.owner = f"test-{uuid4()}"
        self.other = f"test-{uuid4()}"
        self.addAsyncCleanup(self.cleanup)
        self.tasks = PostgresTaskRepository(self.sessions)
        self.meetings = PostgresMeetingRepository(self.sessions)
        self.reminders = PostgresReminderRepository(self.sessions)
        self.task_service = TaskService(repository=self.tasks, clock=lambda: CREATED)
        self.meeting_service = MeetingService(repository=self.meetings, clock=lambda: CREATED)
        self.reminder_service = ReminderService(repository=self.reminders, clock=lambda: CREATED)

    async def cleanup(self) -> None:
        async with self.sessions() as session:
            owners = (self.owner, self.other)
            await session.execute(delete(operations).where(operations.c.owner_id.in_(owners)))
            await session.execute(delete(events).where(events.c.owner_id.in_(owners)))
            await session.commit()

    async def count_rows(self, table=events) -> int:  # type: ignore[no-untyped-def]
        async with self.sessions() as session:
            return int(
                await session.scalar(
                    select(func.count()).select_from(table).where(table.c.owner_id == self.owner)
                )
                or 0
            )

    async def test_records_survive_a_new_engine(self) -> None:
        task = await self.task_service.create_task(
            owner_id=self.owner,
            title=" Отчёт ",
            description="Квартальный",
            due_date=date(2026, 10, 24),
            operation_id="t-1",
        )
        exact = await self.task_service.create_task(
            owner_id=self.owner,
            title="Звонок",
            due_at=datetime(2026, 10, 24, 15, tzinfo=UTC),
            operation_id="t-2",
        )
        meeting = await self.meeting_service.create_meeting(
            owner_id=self.owner, title="Встреча", starts_at=CREATED + timedelta(hours=1), operation_id="m-1"
        )
        reminder = await self.reminder_service.create_reminder(
            owner_id=self.owner,
            text="Позвонить",
            remind_at=CREATED + timedelta(hours=2),
            description="Про договор",
            operation_id="r-1",
        )
        # A new engine and pool cannot rely on the first one's state (restart).
        async with postgres_engine(integration_settings()) as engine:
            sessions = session_factory(engine)
            self.assertEqual(
                await PostgresTaskRepository(sessions).get(task_id=task.id, owner_id=self.owner), task
            )
            self.assertEqual(
                await PostgresTaskRepository(sessions).get(task_id=exact.id, owner_id=self.owner), exact
            )
            self.assertEqual(
                await PostgresMeetingRepository(sessions).get(meeting_id=meeting.id, owner_id=self.owner),
                meeting,
            )
            self.assertEqual(
                await PostgresReminderRepository(sessions).get(reminder_id=reminder.id, owner_id=self.owner),
                reminder,
            )
        self.assertEqual(task.title, "Отчёт")
        self.assertEqual(task.created_at.microsecond, 123456)
        self.assertIsNone(meeting.ends_at)

    async def test_reschedule_is_atomic_idempotent_owner_scoped_and_updates_period(self) -> None:
        original = await self.meeting_service.create_meeting(
            owner_id=self.owner,
            title="Хакатон",
            starts_at=CREATED,
            ends_at=CREATED + timedelta(hours=4),
            operation_id="meeting",
        )
        args = {
            "meeting_id": original.id,
            "owner_id": self.owner,
            "starts_at": CREATED + timedelta(days=1),
            "operation_id": "move",
        }
        moved = await asyncio.gather(*(self.meeting_service.reschedule_meeting(**args) for _ in range(6)))
        self.assertTrue(all(record == moved[0] for record in moved))
        self.assertEqual(
            moved[0],
            replace(original, starts_at=args["starts_at"], ends_at=args["starts_at"] + timedelta(hours=4)),
        )
        self.assertEqual(await self.count_rows(), 1)
        self.assertEqual(await self.count_rows(operations), 2)
        self.assertIsNone(await self.meeting_service.reschedule_meeting(**{**args, "owner_id": self.other}))
        with self.assertRaises(OperationConflictError):
            await self.meeting_service.reschedule_meeting(**{**args, "starts_at": CREATED})
        self.assertEqual(
            await self.meetings.list(
                RecordQuery(owner_id=self.owner, start_time=CREATED, end_time=CREATED + timedelta(days=1))
            ),
            [],
        )
        new_repo = PostgresMeetingRepository(self.sessions)
        self.assertEqual(await new_repo.get(meeting_id=original.id, owner_id=self.owner), moved[0])
        # Replaying an older action must not undo a more recent move.
        later = await self.meeting_service.reschedule_meeting(
            **{**args, "starts_at": CREATED + timedelta(days=2), "operation_id": "move-2"}
        )
        self.assertEqual(await self.meeting_service.reschedule_meeting(**args), later)

    async def test_failed_reschedule_does_not_save_operation_or_time(self) -> None:
        original = await self.meeting_service.create_meeting(
            owner_id=self.owner,
            title="Хакатон",
            starts_at=CREATED,
            ends_at=CREATED + timedelta(hours=1),
            operation_id="meeting",
        )
        args = {"meeting_id": original.id, "owner_id": self.owner, "operation_id": "bad"}
        with self.assertRaises(ValueError):
            await self.meeting_service.reschedule_meeting(**args, ends_at=CREATED)
        self.assertEqual(await self.count_rows(operations), 1)
        self.assertEqual(await self.meetings.get(meeting_id=original.id, owner_id=self.owner), original)
        # Reusing a creation action for a move conflicts without changing the event.
        with self.assertRaises(OperationConflictError):
            await self.meeting_service.reschedule_meeting(
                **{**args, "operation_id": "meeting"}, starts_at=CREATED + timedelta(days=1)
            )
        self.assertEqual(await self.meetings.get(meeting_id=original.id, owner_id=self.owner), original)

    async def test_stale_reschedule_cannot_overwrite_a_later_change(self) -> None:
        original = await self.meeting_service.create_meeting(
            owner_id=self.owner,
            title="Хакатон",
            starts_at=CREATED,
            operation_id="meeting",
        )
        current = await self.meeting_service.reschedule_meeting(
            meeting_id=original.id,
            owner_id=self.owner,
            starts_at=CREATED + timedelta(days=1),
            operation_id="move",
        )
        with self.assertRaises(OperationConflictError):
            await self.meetings.reschedule(
                replace(original, starts_at=CREATED + timedelta(days=2)),
                previous=original,
                operation=CreationOperation.from_data("stale", meeting_id=str(original.id)),
                changed_at=CREATED,
            )
        self.assertEqual(await self.meetings.get(meeting_id=original.id, owner_id=self.owner), current)
        self.assertEqual(await self.count_rows(operations), 2)

    async def test_owner_and_type_isolation(self) -> None:
        task = await self.task_service.create_task(owner_id=self.owner, title="Отчёт", operation_id="t-1")
        self.assertIsNone(await self.tasks.get(task_id=task.id, owner_id=self.other))
        self.assertEqual(await self.tasks.list(RecordQuery(owner_id=self.other)), [])
        # The same UUID is not a meeting or a reminder.
        self.assertIsNone(await self.meetings.get(meeting_id=task.id, owner_id=self.owner))
        self.assertIsNone(await self.reminders.get(reminder_id=task.id, owner_id=self.owner))
        self.assertEqual(await self.meetings.list(RecordQuery(owner_id=self.owner)), [])
        self.assertIsNone(await self.tasks.get(task_id=uuid4(), owner_id=self.owner))

    async def test_explicit_sort_is_applied_before_limit_for_every_order(self) -> None:
        stored = []
        for index, (name, minutes) in enumerate([("Альфа", 20), ("Бета", 240), ("Гамма", None)]):
            record = create_meeting(
                owner_id=self.owner,
                title=name,
                starts_at=CREATED + timedelta(hours=index),
                created_at=CREATED + timedelta(seconds=index),
                ends_at=None if minutes is None else CREATED + timedelta(hours=index, minutes=minutes),
            )
            await self.meetings.add(record)
            stored.append(record)
        await self.meetings.add(
            create_meeting(
                owner_id=self.other,
                title="Чужая",
                starts_at=CREATED + timedelta(days=10),
                ends_at=CREATED + timedelta(days=11),
                created_at=CREATED,
            )
        )
        for sort, expected in {
            "time_asc": [0, 1, 2],
            "time_desc": [2, 1, 0],
            "duration_asc": [0, 1, 2],
            "duration_desc": [1, 0, 2],
            "name_asc": [0, 1, 2],
            "name_desc": [2, 1, 0],
            "created_asc": [0, 1, 2],
            "created_desc": [2, 1, 0],
        }.items():
            with self.subTest(sort=sort):
                rows = await self.meetings.list(RecordQuery(owner_id=self.owner, sort=sort, limit=1))
                self.assertEqual(rows, [stored[expected[0]]])
                rows = await self.meetings.list(
                    RecordQuery(owner_id=self.owner, sort=sort, limit=2, offset=1)
                )
                self.assertEqual(rows, [stored[i] for i in expected[1:]])

    async def test_explicit_task_time_sort_uses_deadline_and_puts_undated_last(self) -> None:
        undated = await self.task_service.create_task(
            owner_id=self.owner, title="Без срока", operation_id="u"
        )
        dated = await self.task_service.create_task(
            owner_id=self.owner, title="Дата", operation_id="d", due_date=date(2026, 10, 27)
        )
        exact = await self.task_service.create_task(
            owner_id=self.owner,
            title="Момент",
            operation_id="e",
            due_at=datetime(2026, 10, 26, 23, tzinfo=UTC),
        )
        rows = await self.tasks.list(
            RecordQuery(owner_id=self.owner, sort="time_asc", timezone="Europe/Moscow")
        )
        self.assertEqual(rows, [dated, exact, undated])
        rows = await self.tasks.list(
            RecordQuery(owner_id=self.owner, sort="time_desc", timezone="Europe/Moscow")
        )
        self.assertEqual(rows, [exact, dated, undated])

    async def test_replay_conflict_and_single_row(self) -> None:
        first = await self.task_service.create_task(owner_id=self.owner, title="Отчёт", operation_id="t-1")
        later = TaskService(repository=self.tasks, clock=lambda: CREATED + timedelta(days=1))
        self.assertEqual(
            await later.create_task(owner_id=self.owner, title="Отчёт", operation_id="t-1"), first
        )
        with self.assertRaises(OperationConflictError):
            await later.create_task(owner_id=self.owner, title="Другое", operation_id="t-1")
        # Another owner has its own namespace; one action_id of one owner is one action of any type.
        other = await later.create_task(owner_id=self.other, title="Отчёт", operation_id="t-1")
        self.assertNotEqual(other.id, first.id)
        with self.assertRaises(OperationConflictError):
            await self.meeting_service.create_meeting(
                owner_id=self.owner, title="Отчёт", starts_at=CREATED + timedelta(hours=1), operation_id="t-1"
            )
        self.assertEqual(await self.count_rows(), 1)
        self.assertEqual(await self.count_rows(operations), 1)

    async def test_concurrent_repeats_create_one_record(self) -> None:
        results = await asyncio.gather(
            *(
                self.meeting_service.create_meeting(
                    owner_id=self.owner,
                    title="Встреча",
                    starts_at=CREATED + timedelta(hours=1),
                    operation_id="m-1",
                )
                for _ in range(8)
            )
        )
        self.assertEqual(len({meeting.id for meeting in results}), 1)
        self.assertEqual(await self.count_rows(), 1)
        self.assertEqual(await self.count_rows(operations), 1)

    async def test_existing_id_is_never_overwritten(self) -> None:
        task = create_task(owner_id=self.owner, title="Отчёт", created_at=CREATED)
        await self.tasks.add(task)
        with self.assertRaises(TaskAlreadyExistsError):
            await self.tasks.add(replace(task, owner_id=self.other, title="Замена"))
        meeting = create_meeting(
            owner_id=self.owner, title="Встреча", starts_at=CREATED + timedelta(hours=1), created_at=CREATED
        )
        await self.meetings.add(meeting)
        with self.assertRaises(MeetingAlreadyExistsError):
            await self.meetings.add(meeting)
        self.assertEqual(await self.tasks.get(task_id=task.id, owner_id=self.owner), task)

    async def test_status_update(self) -> None:
        clock_times = iter([CREATED + timedelta(minutes=minute) for minute in range(1, 10)])
        tasks = PostgresTaskRepository(self.sessions, clock=lambda: next(clock_times))
        service = TaskService(repository=tasks, clock=lambda: CREATED)
        task = await service.create_task(owner_id=self.owner, title="Отчёт", operation_id="t-1")
        self.assertIsNone(await service.update_status(task_id=task.id, owner_id=self.other, status="done"))
        self.assertIsNone(await service.update_status(task_id=uuid4(), owner_id=self.owner, status="done"))
        for status in ("in_progress", "done", "todo"):
            updated = await service.update_status(task_id=task.id, owner_id=self.owner, status=status)
            assert updated is not None
            self.assertEqual((updated.id, updated.status, updated.created_at), (task.id, status, CREATED))
        async with self.sessions() as session:
            stamp = await session.scalar(select(events.c.updated_at).where(events.c.id == task.id))
        # Repeating the current status writes nothing.
        await service.update_status(task_id=task.id, owner_id=self.owner, status="todo")
        async with self.sessions() as session:
            self.assertEqual(
                await session.scalar(select(events.c.updated_at).where(events.c.id == task.id)), stamp
            )
        # updated_at comes from the repository clock, created_at stays the creation time.
        self.assertGreater(stamp, CREATED)
        replay = await service.create_task(owner_id=self.owner, title="Отчёт", operation_id="t-1")
        self.assertEqual(replay.status, "todo")
        # A meeting has no status: the task operation never touches it.
        meeting = await self.meeting_service.create_meeting(
            owner_id=self.owner, title="Встреча", starts_at=CREATED + timedelta(hours=1), operation_id="m-1"
        )
        self.assertIsNone(await tasks.update_status(task_id=meeting.id, owner_id=self.owner, status="done"))

    async def test_half_open_period_order_and_pages(self) -> None:
        start = datetime(2026, 10, 24, 9, tzinfo=UTC)
        moments = [
            start - timedelta(microseconds=1),
            start,
            start + timedelta(hours=1),
            start + timedelta(hours=2),
        ]
        for index, moment in enumerate(moments):
            await self.meeting_service.create_meeting(
                owner_id=self.owner, title=f"Встреча {index}", starts_at=moment, operation_id=f"m-{index}"
            )
        period = RecordQuery(owner_id=self.owner, start_time=start, end_time=start + timedelta(hours=2))
        found = await self.meetings.list(period)
        self.assertEqual([meeting.title for meeting in found], ["Встреча 1", "Встреча 2"])
        page = await self.meetings.list(RecordQuery(owner_id=self.owner, limit=2, offset=1))
        self.assertEqual([meeting.title for meeting in page], ["Встреча 1", "Встреча 2"])
        self.assertEqual(
            await self.meetings.list(RecordQuery(owner_id=self.owner, start_time=start + timedelta(days=1))),
            [],
        )

    async def test_local_day_with_dst_transition(self) -> None:
        # 2026-10-25 in Berlin lasts 25 hours; 23:30 local is still inside that day.
        start, end = day_bounds_utc(date(2026, 10, 25), "Europe/Berlin")
        inside = datetime(2026, 10, 25, 22, 30, tzinfo=UTC)
        await self.reminder_service.create_reminder(
            owner_id=self.owner, text="Поздно", remind_at=inside, operation_id="r-1"
        )
        await self.reminder_service.create_reminder(
            owner_id=self.owner, text="Завтра", remind_at=end, operation_id="r-2"
        )
        found = await self.reminders.list(RecordQuery(owner_id=self.owner, start_time=start, end_time=end))
        self.assertEqual([reminder.text for reminder in found], ["Поздно"])
        self.assertEqual(end - start, timedelta(hours=25))

    async def test_tasks_period_filters_creation_time(self) -> None:
        early = TaskService(repository=self.tasks, clock=lambda: CREATED - timedelta(days=1))
        await early.create_task(
            owner_id=self.owner, title="Вчерашняя", due_date=date(2026, 10, 23), operation_id="t-1"
        )
        await self.task_service.create_task(owner_id=self.owner, title="Сегодняшняя", operation_id="t-2")
        today = RecordQuery(owner_id=self.owner, start_time=CREATED - timedelta(hours=1))
        self.assertEqual([task.title for task in await self.tasks.list(today)], ["Сегодняшняя"])

    async def test_russian_full_text_search(self) -> None:
        await self.meeting_service.create_meeting(
            owner_id=self.owner,
            title="Встреча с Иваном",
            description="Обсудить бюджет",
            starts_at=CREATED + timedelta(hours=1),
            operation_id="m-1",
        )
        await self.meeting_service.create_meeting(
            owner_id=self.other,
            title="Встреча с Олегом",
            starts_at=CREATED + timedelta(hours=1),
            operation_id="m-1",
        )
        await self.task_service.create_task(
            owner_id=self.owner, title="Подготовить бюджет", operation_id="t-1"
        )
        for query, expected in {
            "встречу": ["Встреча с Иваном"],
            "бюджета": ["Встреча с Иваном"],
            "отчёт": [],
        }.items():
            with self.subTest(query=query):
                found = await self.meetings.list(RecordQuery(owner_id=self.owner, query=query))
                self.assertEqual([meeting.title for meeting in found], expected)
        tasks = await self.tasks.list(RecordQuery(owner_id=self.owner, query="бюджеты"))
        self.assertEqual([task.title for task in tasks], ["Подготовить бюджет"])

    async def test_tasks_by_deadline_period(self) -> None:
        zone = "Europe/Moscow"
        start, end = day_bounds_utc(date(2026, 10, 24), zone)
        for title, due in (
            ("Датой сегодня", {"due_date": date(2026, 10, 24)}),
            ("Датой завтра", {"due_date": date(2026, 10, 25)}),
            ("Ко времени сегодня", {"due_at": start + timedelta(hours=15)}),
            ("Ровно в конце", {"due_at": end}),
            ("Без срока", {}),
        ):
            await self.task_service.create_task(owner_id=self.owner, title=title, operation_id=title, **due)
        query = RecordQuery(
            owner_id=self.owner, start_time=start, end_time=end, task_period="due", timezone=zone
        )
        # Date-only deadline sorts at its local midnight, before the 15:00 deadline.
        self.assertEqual(
            [task.title for task in await self.tasks.list(query)], ["Датой сегодня", "Ко времени сегодня"]
        )
        week = RecordQuery(owner_id=self.owner, start_time=start, task_period="due", timezone=zone)
        titles = [task.title for task in await self.tasks.list(week)]
        self.assertEqual(titles[:2], ["Датой сегодня", "Ко времени сегодня"])
        # Both remaining deadlines are the next local midnight; the UUID breaks the tie.
        self.assertCountEqual(titles[2:], ["Датой завтра", "Ровно в конце"])
        searched = await self.tasks.list(
            RecordQuery(
                owner_id=self.owner,
                start_time=start,
                end_time=end,
                task_period="due",
                timezone=zone,
                query="сегодня",
            )
        )
        self.assertEqual([task.title for task in searched], ["Датой сегодня", "Ко времени сегодня"])
