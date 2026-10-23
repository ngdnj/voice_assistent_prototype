import os
import unittest
from unittest.mock import patch

from app.storage.postgres.settings import PostgresSettings
from tests.storage_pg import integration_settings


class PostgresSettingsTests(unittest.TestCase):
    def test_defaults_without_environment(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            settings = PostgresSettings.from_env()
        self.assertEqual((settings.host, settings.port), ("127.0.0.1", 5433))
        self.assertEqual(
            (settings.database, settings.user, settings.password), ("it_gorky", "it_gorky", None)
        )

    def test_environment_settings(self) -> None:
        environment = {
            "POSTGRES_HOST": "db",
            "POSTGRES_PORT": "6543",
            "POSTGRES_DB": "assistant",
            "POSTGRES_USER": "app",
            "POSTGRES_PASSWORD": "example-only",
            "POSTGRES_CONNECT_TIMEOUT": "1.5",
            "POSTGRES_COMMAND_TIMEOUT": "2.5",
            "POSTGRES_POOL_SIZE": "3",
        }
        with patch.dict(os.environ, environment, clear=True):
            settings = PostgresSettings.from_env()
        self.assertEqual(
            (settings.host, settings.port, settings.database, settings.user),
            ("db", 6543, "assistant", "app"),
        )
        self.assertEqual(
            (settings.connect_timeout, settings.command_timeout, settings.pool_size), (1.5, 2.5, 3)
        )
        self.assertNotIn("example-only", repr(settings))
        self.assertNotIn("example-only", str(settings.url()))
        self.assertEqual(settings.url().password, "example-only")
        self.assertEqual(settings.url().drivername, "postgresql+asyncpg")

    def test_invalid_values(self) -> None:
        for values in (
            {"host": " "},
            {"database": ""},
            {"user": "\t"},
            {"port": 0},
            {"port": 65536},
            {"connect_timeout": 0},
            {"command_timeout": float("nan")},
            {"connect_timeout": float("inf")},
            {"pool_size": 0},
            {"pool_size": 51},
        ):
            with self.subTest(values=values), self.assertRaises(ValueError):
                PostgresSettings(**values)  # type: ignore[arg-type]

    def test_integration_settings_require_test_database(self) -> None:
        for database in ("", "it_gorky", "production", "latest", "attestation", "it_gorky_testing"):
            with (
                self.subTest(database=database),
                patch.dict(os.environ, {"POSTGRES_TEST_DB": database}, clear=True),
                self.assertRaises(RuntimeError),
            ):
                integration_settings()
        for database in ("it_gorky_test", "test", "test_it_gorky", "it_gorky_test_2"):
            with patch.dict(os.environ, {"POSTGRES_TEST_DB": database}, clear=True):
                self.assertEqual(integration_settings().database, database)
