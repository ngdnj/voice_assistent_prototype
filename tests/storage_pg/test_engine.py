import unittest

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.storage.postgres.engine import check_postgres, postgres_engine, session_factory
from app.storage.postgres.settings import PostgresSettings
from tests.storage_pg import integration_settings, requires_postgres


class EngineWithoutServerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unreachable_server_fails_fast(self) -> None:
        # Port 1 on localhost is closed; the engine must raise instead of hanging and still dispose.
        settings = PostgresSettings(port=1, password="unused", connect_timeout=1.0)
        with self.assertRaises((SQLAlchemyError, OSError, TimeoutError)):
            async with postgres_engine(settings):
                self.fail("engine must not be yielded without a reachable server")


@requires_postgres
class PostgresEngineIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_check_and_session_roundtrip(self) -> None:
        async with postgres_engine(integration_settings()) as engine:
            self.assertTrue(await check_postgres(engine))
            sessions = session_factory(engine)
            async with sessions() as session:
                database = await session.scalar(text("SELECT current_database()"))
                version = await session.scalar(text("SHOW server_version_num"))
        self.assertIn("test", database)
        self.assertGreaterEqual(int(version), 170000)

    async def test_engine_is_disposed_after_exit(self) -> None:
        async with postgres_engine(integration_settings()) as engine:
            pass
        self.assertEqual(engine.pool.checkedout(), 0)  # type: ignore[attr-defined]
