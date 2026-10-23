import unittest
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from uuid import UUID

from app.planner.task import Task, create_task


CREATED_AT = datetime(2026, 10, 21, 9, 30, tzinfo=UTC)


class UndefinedOffset(tzinfo):
    def utcoffset(self, dt: datetime | None) -> None:
        return None


class TaskTests(unittest.TestCase):
    def test_create_task(self) -> None:
        task = create_task(
            owner_id="user-42", title="Подготовить отчёт", created_at=CREATED_AT
        )
        self.assertIsInstance(task.id, UUID)
        self.assertEqual(task.owner_id, "user-42")
        self.assertEqual(task.title, "Подготовить отчёт")
        self.assertEqual(task.status, "todo")

    def test_strip_outer_title_whitespace(self) -> None:
        task = create_task(
            owner_id="user-42", title=" \t Отправить  отчёт \n", created_at=CREATED_AT
        )
        self.assertEqual(task.title, "Отправить  отчёт")

    def test_reject_blank_values(self) -> None:
        for field_name in ("owner_id", "title"):
            for value in ("", " ", "\t\n", "\u00a0"):
                with self.subTest(field=field_name, value=value):
                    values = {"owner_id": "user-42", "title": "Отчёт"}
                    values[field_name] = value
                    with self.assertRaisesRegex(ValueError, field_name):
                        create_task(**values, created_at=CREATED_AT)

    def test_restore_allowed_statuses(self) -> None:
        for status in ("todo", "in_progress", "done"):
            with self.subTest(status=status):
                task = Task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT, status=status)
                self.assertEqual(task.status, status)

    def test_reject_unknown_status(self) -> None:
        for status in ("active", "TODO", "", "cancelled"):
            with self.subTest(status=status), self.assertRaisesRegex(ValueError, "status"):
                Task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT, status=status)  # type: ignore[arg-type]

    def test_new_tasks_have_different_ids(self) -> None:
        first = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        second = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        self.assertNotEqual(first.id, second.id)

    def test_preserve_explicit_utc_time(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        self.assertEqual(task.created_at, CREATED_AT)
        self.assertIs(task.created_at.tzinfo, UTC)

    def test_normalize_aware_time_to_utc(self) -> None:
        local_time = datetime(2026, 10, 21, 12, 30, tzinfo=timezone(timedelta(hours=3)))
        task = create_task(owner_id="user-42", title="Отчёт", created_at=local_time)
        self.assertEqual(task.created_at, CREATED_AT)
        self.assertIs(task.created_at.tzinfo, UTC)

    def test_reject_naive_time(self) -> None:
        for value in (
            datetime(2026, 10, 21, 9, 30),
            datetime(2026, 10, 21, 9, 30, tzinfo=UndefinedOffset()),
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(ValueError, "timezone-aware"):
                    create_task(owner_id="user-42", title="Отчёт", created_at=value)

    def test_direct_construction_checks_rules(self) -> None:
        with self.assertRaisesRegex(ValueError, "title"):
            Task(owner_id="user-42", title=" ", created_at=CREATED_AT)

    def test_task_is_immutable(self) -> None:
        task = create_task(owner_id="user-42", title="Отчёт", created_at=CREATED_AT)
        with self.assertRaises(FrozenInstanceError):
            setattr(task, "title", "")
