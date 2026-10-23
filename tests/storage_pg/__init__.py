"""Role 4 PostgreSQL tests; integration tests run only against a dedicated test database."""

import os
import unittest
from dataclasses import replace

from app.storage.postgres.settings import PostgresSettings

RUN_PG_INTEGRATION = os.environ.get("RUN_PG_INTEGRATION") == "1"

requires_postgres = unittest.skipUnless(
    RUN_PG_INTEGRATION, "set RUN_PG_INTEGRATION=1 and POSTGRES_TEST_DB for a dedicated test PostgreSQL"
)


def integration_settings() -> PostgresSettings:
    """Connection settings for the test database; refuses anything not named as a test DB."""
    database = os.environ.get("POSTGRES_TEST_DB", "")
    if "test" not in database:
        raise RuntimeError("POSTGRES_TEST_DB must name a dedicated test database (containing 'test')")
    return replace(PostgresSettings.from_env(), database=database)
