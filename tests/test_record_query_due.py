import unittest
from datetime import UTC, date, datetime, timedelta

from app.planner.meeting import create_meeting
from app.planner.query import RecordQuery
from app.planner.reminder import Reminder, create_reminder
from app.planner.task import Task, create_task
from app.planner.time import day_bounds_utc
from app.storage.memory import InMemoryTaskRepository

CREATED = datetime(2026, 10, 23, 8, tzinfo=UTC)


class NameLimitTests(unittest.TestCase):
    def test_creation_rejects_names_over_200_characters(self) -> None:
        name = "я" * 200
        self.assertEqual(create_task(owner_id="u", title=f"  {name}  ", created_at=CREATED).title, name)
        with self.assertRaisesRegex(ValueError, "title"):
            create_task(owner_id="u", title=name + "я", created_at=CREATED)
        with self.assertRaisesRegex(ValueError, "title"):
            create_meeting(owner_id="u", title=name + "я", starts_at=CREATED, created_at=CREATED)
        with self.assertRaisesRegex(ValueError, "text"):
            create_reminder(owner_id="u", text=name + "я", remind_at=CREATED, created_at=CREATED)

    def test_stored_long_names_remain_readable(self) -> None:
        # The limit applies to new records only; restoring older data must not fail.
        self.assertEqual(len(Task(owner_id="u", title="я" * 300, created_at=CREATED).title), 300)
        self.assertEqual(
            len(Reminder(owner_id="u", text="я" * 300, remind_at=CREATED, created_at=CREATED).text), 300
        )


class TaskPeriodQueryTests(unittest.TestCase):
    def test_defaults_keep_creation_period(self) -> None:
        query = RecordQuery(owner_id="u")
        self.assertEqual((query.task_period, query.timezone), ("created_at", None))

    def test_due_period_requires_known_timezone(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone"):
            RecordQuery(owner_id="u", task_period="due")
        with self.assertRaisesRegex(ValueError, "timezone"):
            RecordQuery(owner_id="u", task_period="due", timezone="Mars/Olympus")
        with self.assertRaisesRegex(ValueError, "task_period"):
            RecordQuery(owner_id="u", task_period="deadline")  # type: ignore[arg-type]

    def test_due_date_bounds_follow_local_midnights(self) -> None:
        start, end = day_bounds_utc(date(2026, 10, 24), "Asia/Yekaterinburg")
        query = RecordQuery(
            owner_id="u", start_time=start, end_time=end, task_period="due", timezone="Asia/Yekaterinburg"
        )
        self.assertEqual(query.due_date_bounds(), (date(2026, 10, 24), date(2026, 10, 25)))
        # A few hours inside one day contain no local midnight: no date-only deadline matches.
        hours = RecordQuery(
            owner_id="u",
            start_time=start + timedelta(hours=9),
            end_time=start + timedelta(hours=12),
            task_period="due",
            timezone="Asia/Yekaterinburg",
        )
        first, last = hours.due_date_bounds()
        self.assertEqual(first, last)
        open_end = RecordQuery(
            owner_id="u", start_time=start, task_period="due", timezone="Asia/Yekaterinburg"
        )
        self.assertEqual(open_end.due_date_bounds(), (date(2026, 10, 24), None))


class DeadlinePeriodUnsupportedTests(unittest.IsolatedAsyncioTestCase):
    async def test_memory_storage_refuses_instead_of_answering_wrongly(self) -> None:
        query = RecordQuery(owner_id="u", task_period="due", timezone="Europe/Moscow")
        with self.assertRaisesRegex(ValueError, "created_at"):
            await InMemoryTaskRepository().list(query)
