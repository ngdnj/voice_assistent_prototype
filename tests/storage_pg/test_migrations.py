import asyncio
import io
import re
import unittest
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from sqlalchemy import insert, inspect, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection
from sqlalchemy.schema import CreateTable

from app.storage.postgres.engine import postgres_engine
from app.storage.postgres.settings import PostgresSettings
from app.storage.postgres.tables import events, metadata, operations
from tests.storage_pg import integration_settings, requires_postgres

ROOT = Path(__file__).resolve().parents[2]
CREATED = datetime(2026, 10, 23, 8, tzinfo=UTC)


def alembic_config(settings: PostgresSettings) -> Config:
    # Offline SQL goes to output_buffer, command messages to stdout; neither is printed.
    config = Config(str(ROOT / "alembic.ini"), output_buffer=io.StringIO(), stdout=io.StringIO())
    config.attributes["postgres_settings"] = settings
    config.attributes["configure_logger"] = False
    return config


def event_row(**changes: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": uuid4(),
        "owner_id": "user-42",
        "event_type": "task",
        "name": "Отчёт",
        "description": "",
        "status": "todo",
        "due_date": None,
        "scheduled_at": None,
        "ends_at": None,
        "created_at": CREATED,
        "updated_at": CREATED,
    }
    row.update(changes)
    return row


MEETING = {"event_type": "meeting", "status": None, "scheduled_at": CREATED + timedelta(hours=1)}
REMINDER = {"event_type": "reminder", "status": None, "scheduled_at": CREATED + timedelta(hours=1)}


class OfflineMigrationTests(unittest.TestCase):
    def test_migration_renders_sql_without_database(self) -> None:
        config = alembic_config(PostgresSettings(password="unused"))
        command.upgrade(config, "head", sql=True)
        sql = config.output_buffer.getvalue()  # type: ignore[union-attr]
        for statement in (
            "CREATE TABLE events",
            "CREATE TABLE operations",
            "USING gin (search)",
            "to_tsvector('russian', name || ' ' || description)",
        ):
            self.assertIn(statement, sql)
        self.assertNotIn("unused", sql)


@requires_postgres
class MigrationIntegrationTests(unittest.TestCase):
    """Runs on the dedicated test database only; leaves it migrated to head."""

    def setUp(self) -> None:
        self.settings = integration_settings()
        self.config = alembic_config(self.settings)
        command.downgrade(self.config, "base")
        command.upgrade(self.config, "head")

    def run_db(self, action: Callable[[AsyncConnection], Awaitable[Any]]) -> Any:
        async def run() -> Any:
            async with postgres_engine(self.settings) as engine, engine.connect() as connection:
                return await action(connection)

        return asyncio.run(run())

    def table_names(self) -> set[str]:
        async def names(connection: AsyncConnection) -> set[str]:
            return set(await connection.run_sync(lambda sync: inspect(sync).get_table_names()))

        result: set[str] = self.run_db(names)
        return result

    def test_upgrade_downgrade_upgrade(self) -> None:
        self.assertTrue({"events", "operations"} <= self.table_names())
        command.downgrade(self.config, "base")
        self.assertFalse({"events", "operations"} & self.table_names())
        command.upgrade(self.config, "head")
        self.assertTrue({"events", "operations"} <= self.table_names())

    def test_migrated_schema_matches_metadata(self) -> None:
        def diff(sync: Connection) -> list[Any]:
            return list(compare_metadata(MigrationContext.configure(sync), metadata))

        async def compare(connection: AsyncConnection) -> list[Any]:
            return await connection.run_sync(diff)

        self.assertEqual(self.run_db(compare), [])

    def test_constraint_names_match_metadata(self) -> None:
        # compare_metadata ignores CHECK constraints, so compare their names explicitly.
        dialect = postgresql.dialect()
        expected = {
            table.name: set(
                re.findall(r"CONSTRAINT (ck_\w+) CHECK", str(CreateTable(table).compile(dialect=dialect)))
            )
            for table in (events, operations)
        }

        async def actual(connection: AsyncConnection) -> dict[str, set[str]]:
            def names(sync: Connection) -> dict[str, set[str]]:
                inspector = inspect(sync)
                return {
                    name: {str(check["name"]) for check in inspector.get_check_constraints(name)}
                    for name in expected
                }

            return await connection.run_sync(names)

        self.assertEqual(self.run_db(actual), expected)
        self.assertEqual(len(expected["events"]), 11)

    def test_valid_rows_are_accepted(self) -> None:
        rows = [
            event_row(),
            event_row(status="in_progress", due_date=date(2026, 10, 24)),
            event_row(status="done", scheduled_at=CREATED + timedelta(days=1)),
            event_row(scheduled_at=CREATED, ends_at=CREATED + timedelta(hours=1)),
            event_row(**MEETING),
            event_row(**MEETING, ends_at=CREATED + timedelta(hours=2)),
            event_row(**{**REMINDER, "scheduled_at": CREATED}),
        ]

        async def insert_all(connection: AsyncConnection) -> int:
            await connection.execute(insert(events), rows)
            await connection.execute(
                insert(operations).values(
                    owner_id="user-42",
                    operation_type="create_task",
                    operation_id="op-1",
                    fingerprint="a" * 64,
                    event_id=rows[0]["id"],
                    created_at=CREATED,
                )
            )
            count = await connection.scalar(select(text("count(*)")).select_from(events))
            await connection.rollback()
            return int(count or 0)

        self.assertEqual(self.run_db(insert_all), len(rows))

    def test_check_constraints_reject_invalid_rows(self) -> None:
        invalid = {
            "unknown type": event_row(event_type="note", status=None),
            "blank owner": event_row(owner_id="  "),
            "blank name": event_row(name=" "),
            "long name": event_row(name="я" * 201),
            "long description": event_row(description="я" * 2001),
            "task without status": event_row(status=None),
            "unknown status": event_row(status="active"),
            "meeting with status": event_row(**{**MEETING, "status": "todo"}),
            "date and moment": event_row(due_date=date(2026, 10, 24), scheduled_at=CREATED),
            "meeting with date": event_row(**MEETING, due_date=date(2026, 10, 24)),
            "meeting without moment": event_row(**{**MEETING, "scheduled_at": None}),
            "reminder without moment": event_row(**{**REMINDER, "scheduled_at": None}),
            "end before start": event_row(**MEETING, ends_at=CREATED),
            "end equals start": event_row(**MEETING, ends_at=MEETING["scheduled_at"]),
            "task end without start": event_row(ends_at=CREATED + timedelta(hours=1)),
            "task end equals start": event_row(scheduled_at=CREATED, ends_at=CREATED),
            "reminder with end": event_row(**REMINDER, ends_at=CREATED + timedelta(hours=2)),
            "reminder in the past": event_row(**{**REMINDER, "scheduled_at": CREATED - timedelta(seconds=1)}),
        }

        async def try_insert(connection: AsyncConnection, row: dict[str, Any]) -> bool:
            try:
                await connection.execute(insert(events).values(**row))
            except IntegrityError:
                return False
            finally:
                await connection.rollback()
            return True

        for case, row in invalid.items():
            with self.subTest(case=case):
                self.assertFalse(self.run_db(lambda connection, row=row: try_insert(connection, row)))

    def test_operation_constraints(self) -> None:
        async def try_operation(connection: AsyncConnection, changes: dict[str, Any]) -> bool:
            event = event_row()
            values = {
                "owner_id": "user-42",
                "operation_type": "create_task",
                "operation_id": "op-1",
                "fingerprint": "a" * 64,
                "event_id": event["id"],
                "created_at": CREATED,
                **changes,
            }
            try:
                await connection.execute(insert(events).values(**event))
                await connection.execute(insert(operations).values(**values))
                await connection.execute(insert(operations).values(**values))
            except IntegrityError:
                return False
            finally:
                await connection.rollback()
            return True

        for case, changes in {
            "duplicate key": {},
            "unknown type": {"operation_type": "delete_task"},
            "empty id": {"operation_id": ""},
            "bad fingerprint": {"fingerprint": "Z" * 64},
            "missing event": {"event_id": uuid4()},
        }.items():
            with self.subTest(case=case):
                self.assertFalse(self.run_db(lambda c, changes=changes: try_operation(c, changes)))

    def test_russian_full_text_search(self) -> None:
        rows = [
            event_row(**MEETING, name="Встреча с Иваном", description="Обсудить бюджет"),
            event_row(name="Купить молоко"),
            event_row(owner_id="other", **MEETING, name="Встреча с Олегом"),
        ]

        async def search(connection: AsyncConnection, query: str) -> list[str]:
            await connection.execute(insert(events), rows)
            found = await connection.scalars(
                select(events.c.name)
                .where(events.c.owner_id == "user-42")
                .where(events.c.search.op("@@")(text("websearch_to_tsquery('russian', :q)")))
                .order_by(events.c.name),
                {"q": query},
            )
            await connection.rollback()
            return list(found)

        for query, expected in {
            "встречу": ["Встреча с Иваном"],
            "бюджета": ["Встреча с Иваном"],
            "молока": ["Купить молоко"],
            "отчёт": [],
        }.items():
            with self.subTest(query=query):
                self.assertEqual(self.run_db(lambda c, query=query: search(c, query)), expected)
