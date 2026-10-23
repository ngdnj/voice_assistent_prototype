"""Role 4 PostgreSQL tests; integration tests run only against a dedicated test database."""

import io
import os
import re
import unittest
from dataclasses import replace
from pathlib import Path

from alembic import command
from alembic.config import Config

from app.storage.postgres.settings import PostgresSettings

RUN_PG_INTEGRATION = os.environ.get("RUN_PG_INTEGRATION") == "1"

requires_postgres = unittest.skipUnless(
    RUN_PG_INTEGRATION, "set RUN_PG_INTEGRATION=1 and POSTGRES_TEST_DB for a dedicated test PostgreSQL"
)


def integration_settings() -> PostgresSettings:
    """Connection settings for the test database; refuses anything not named as a test DB."""
    database = os.environ.get("POSTGRES_TEST_DB", "")
    # "test" must be a separate word of the name: it_gorky_test, test_db; not latest or attestation.
    if not re.fullmatch(r"(?:\w+_)?test(?:_\w+)?", database):
        raise RuntimeError("POSTGRES_TEST_DB must name a dedicated test database, e.g. it_gorky_test")
    return replace(PostgresSettings.from_env(), database=database)


def migrate_test_database() -> None:
    """Upgrade POSTGRES_TEST_DB to head; synchronous, call from setUpClass (outside an event loop)."""
    config = Config(str(Path(__file__).resolve().parents[2] / "alembic.ini"), stdout=io.StringIO())
    config.attributes["postgres_settings"] = integration_settings()
    config.attributes["configure_logger"] = False
    command.upgrade(config, "head")
