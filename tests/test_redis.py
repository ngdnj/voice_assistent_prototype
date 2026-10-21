import asyncio
import os
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from app.storage.redis import RedisSettings, check_redis, redis_connection


class RedisSettingsTests(unittest.TestCase):
    def test_environment_settings(self) -> None:
        with patch.dict(os.environ, {
            "REDIS_HOST": "localhost", "REDIS_PORT": "6380", "REDIS_DB": "3",
            "REDIS_USERNAME": "test", "REDIS_PASSWORD": "example-only",
            "REDIS_CONNECT_TIMEOUT": "1.5", "REDIS_COMMAND_TIMEOUT": "2.5",
        }, clear=True):
            settings = RedisSettings.from_env()
        self.assertEqual(settings.host, "localhost")
        self.assertEqual(settings.port, 6380)
        self.assertEqual(settings.db, 3)
        self.assertEqual(settings.username, "test")
        self.assertEqual(settings.password, "example-only")
        self.assertEqual(settings.connect_timeout, 1.5)
        self.assertEqual(settings.command_timeout, 2.5)
        self.assertNotIn("example-only", repr(settings))

    def test_invalid_timeout(self) -> None:
        for value in (0, -1, float("nan"), float("inf")):
            for field in ("connect_timeout", "command_timeout"):
                with self.subTest(field=field, value=value):
                    with self.assertRaises(ValueError):
                        RedisSettings(**{field: value})

    def test_invalid_address(self) -> None:
        for values in ({"host": " "}, {"port": 0}, {"port": 65536}, {"db": -1}):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    RedisSettings(**values)


class RedisLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_reuse_and_close(self) -> None:
        client = AsyncMock()
        client.ping.return_value = True
        with patch("app.storage.redis.Redis", return_value=client) as factory:
            async with redis_connection(RedisSettings()) as connection:
                self.assertIs(connection, client)
                self.assertTrue(await check_redis(connection))
            factory.assert_called_once()
            self.assertEqual(client.ping.await_count, 2)
            client.aclose.assert_awaited_once()
            self.assertEqual(factory.call_args.kwargs["socket_connect_timeout"], 2)
            self.assertEqual(factory.call_args.kwargs["socket_timeout"], 2)

    async def test_close_on_startup_failure(self) -> None:
        client = AsyncMock()
        error = ConnectionError("Redis unavailable")
        client.ping.side_effect = error
        with patch("app.storage.redis.Redis", return_value=client):
            with self.assertRaises(ConnectionError) as caught:
                async with redis_connection(RedisSettings()):
                    self.fail("unavailable client must not be yielded")
        self.assertIs(caught.exception, error)
        client.aclose.assert_awaited_once()

    async def test_close_on_cancellation(self) -> None:
        client = AsyncMock()
        with patch("app.storage.redis.Redis", return_value=client):
            with self.assertRaises(asyncio.CancelledError):
                async with redis_connection(RedisSettings()):
                    raise asyncio.CancelledError
        client.aclose.assert_awaited_once()

    async def test_close_on_operation_failure(self) -> None:
        client = AsyncMock()
        with patch("app.storage.redis.Redis", return_value=client):
            with self.assertRaisesRegex(RuntimeError, "operation failed"):
                async with redis_connection(RedisSettings()):
                    raise RuntimeError("operation failed")
        client.aclose.assert_awaited_once()


@unittest.skipUnless(
    os.environ.get("RUN_REDIS_INTEGRATION") == "1",
    "set RUN_REDIS_INTEGRATION=1 for a dedicated test Redis",
)
class RedisIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_connection_and_roundtrip(self) -> None:
        key = f"it-gorky:test:redis:{uuid4()}:roundtrip"
        async with redis_connection(RedisSettings.from_env()) as client:
            created = False
            try:
                self.assertTrue(await check_redis(client))
                created = bool(await client.set(key, "привет", ex=60, nx=True))
                self.assertTrue(created)
                self.assertEqual(await client.get(key), "привет")
            finally:
                if created:
                    await client.delete(key)
