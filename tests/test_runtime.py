import asyncio
import json
import logging
import os
import signal
import sys
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
from redis.exceptions import ConnectionError

from app.__main__ import main
from app.planner.query import RecordQuery
from app.runtime import RuntimeSettings, run_backend
from app.storage.redis import RedisSettings, redis_connection
from app.storage.redis_index import read_index


def test_settings_require_local_owner_only_for_example(monkeypatch):
    monkeypatch.delenv("BACKEND_DEMO_OWNER_ID", raising=False)
    assert RuntimeSettings.from_env().demo_owner_id is None
    with pytest.raises(ValueError, match="BACKEND_DEMO_OWNER_ID"):
        RuntimeSettings.from_env(require_demo_owner=True)
    for timeout in ("0", "-1", "nan", "inf", "invalid"):
        monkeypatch.setenv("BACKEND_SHUTDOWN_TIMEOUT", timeout)
        with pytest.raises(ValueError):
            RuntimeSettings.from_env()


def test_missing_setting_fails_before_redis(monkeypatch, caplog):
    monkeypatch.delenv("BACKEND_DEMO_OWNER_ID", raising=False)
    monkeypatch.setattr(sys, "argv", ["app", "example", "--operation-id", str(uuid4())])
    with caplog.at_level(logging.INFO), patch("app.storage.redis.Redis") as factory:
        assert main() == 2
    factory.assert_not_called()
    assert json.loads(caplog.records[-1].message)["event"] == "invalid_settings"


def test_unavailable_redis_does_not_log_credentials(monkeypatch, caplog):
    monkeypatch.setattr(sys, "argv", ["app", "check"])
    client = AsyncMock()
    client.ping.side_effect = ConnectionError("redis://secret:password@host private user text")
    with caplog.at_level(logging.INFO), patch("app.storage.redis.Redis", return_value=client):
        assert main() == 1
    client.aclose.assert_awaited_once()
    assert "storage_unavailable" in caplog.text
    assert "password" not in caplog.text
    assert "private user text" not in caplog.text
    assert "work_completed" not in caplog.text


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.parametrize("finish", [True, False])
async def test_signal_drains_or_cancels_before_client_close(sig, finish, caplog):
    client = AsyncMock()
    operation_id, request_id = uuid4(), uuid4()
    cancelled = False

    async def work(backend):
        nonlocal cancelled
        assert backend.tasks is not None
        os.kill(os.getpid(), sig)
        try:
            if finish:
                await asyncio.sleep(0.01)
            else:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled = True
            client.aclose.assert_not_awaited()
            raise

    with caplog.at_level(logging.INFO), patch("app.storage.redis.Redis", return_value=client):
        result = await run_backend(
            RuntimeSettings(RedisSettings(), shutdown_timeout=0.05),
            work,
            request_id=request_id,
            operation_id=operation_id,
        )
    assert result == (0 if finish else 130)
    assert cancelled is not finish
    client.aclose.assert_awaited_once()
    client.xack.assert_not_awaited()
    events = [json.loads(record.message) for record in caplog.records]
    assert all(event["request_id"] == str(request_id) for event in events)
    assert all(event["operation_id"] == str(operation_id) for event in events)
    assert ("work_completed" in [event["event"] for event in events]) is finish


async def test_failure_propagates_and_closes_client(caplog):
    client = AsyncMock()

    async def work(backend):
        raise RuntimeError("private payload")

    with (
        caplog.at_level(logging.INFO),
        patch("app.storage.redis.Redis", return_value=client),
        pytest.raises(RuntimeError),
    ):
        await run_backend(RuntimeSettings(RedisSettings()), work, request_id=uuid4())
    client.aclose.assert_awaited_once()
    assert "private payload" not in caplog.text
    assert "work_completed" not in caplog.text


@pytest.mark.parametrize("query", [None, "example"])
async def test_index_rejects_client_without_response_decoding(query):
    client = AsyncMock()
    client.zrangebylex.return_value = [b"invalid client configuration"]
    with pytest.raises(ValueError, match="decode_responses"):
        await read_index(client, "test-index", RecordQuery(owner_id="test-owner", query=query))


PROBE = """
import asyncio, os
from uuid import uuid4
from app.runtime import RuntimeSettings, run_backend
async def work(backend):
    stream = backend.notifications
    client = stream.client
    key = os.environ['TEST_STREAM']
    await client.xgroup_create(key, 'test-group', id='0-0')
    await client.xreadgroup('test-group', 'probe', {key: '>'}, count=1)
    print('processing', flush=True)
    await asyncio.sleep(float(os.environ['TEST_WORK_SECONDS']))
    await client.xack(key, 'test-group', os.environ['TEST_ENTRY_ID'])
raise SystemExit(asyncio.run(run_backend(RuntimeSettings.from_env(), work, request_id=uuid4())))
"""


@pytest.mark.skipif(os.environ.get("RUN_REDIS_INTEGRATION") != "1", reason="dedicated Redis required")
@pytest.mark.parametrize("sig", [signal.SIGTERM, signal.SIGINT])
@pytest.mark.parametrize("finish", [False, True])
async def test_real_process_signal_acks_only_finished_work(sig, finish):
    key = f"it-gorky:test:runtime:{uuid4()}"
    async with redis_connection(RedisSettings.from_env()) as client:
        process = None
        try:
            entry_id = await client.xadd(key, {"test": "synthetic"})
            env = dict(
                os.environ,
                TEST_STREAM=key,
                TEST_ENTRY_ID=entry_id,
                TEST_WORK_SECONDS="0.2" if finish else "30",
                BACKEND_SHUTDOWN_TIMEOUT="2" if finish else "0.05",
            )
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-c",
                PROBE,
                env=env,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            assert process.stdout is not None
            assert await asyncio.wait_for(process.stdout.readline(), 5) == b"processing\n"
            process.send_signal(sig)
            output, error = await asyncio.wait_for(process.communicate(), 5)
            assert process.returncode == (0 if finish else 130), (output, error)
            assert (await client.xpending(key, "test-group"))["pending"] == (0 if finish else 1)
        finally:
            if process is not None and process.returncode is None:
                process.kill()
                await process.wait()
            await client.delete(key)


@pytest.mark.skipif(os.environ.get("RUN_REDIS_INTEGRATION") != "1", reason="dedicated Redis required")
async def test_cli_example_can_be_replayed(monkeypatch):
    # These are the exact commands from README, with an isolated owner/operation.
    owner, operation = f"test-runtime-{uuid4()}", uuid4()
    monkeypatch.setenv("BACKEND_DEMO_OWNER_ID", owner)
    env = dict(os.environ)
    ids = []
    async with redis_connection(RedisSettings.from_env()) as client:
        try:
            for _ in range(2):
                process = await asyncio.create_subprocess_exec(
                    sys.executable,
                    "-m",
                    "app",
                    "example",
                    "--operation-id",
                    str(operation),
                    env=env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                output, error = await asyncio.wait_for(process.communicate(), 5)
                assert process.returncode == 0, error
                ids.append(UUID(output.decode().strip().removeprefix("task_id=")))
            assert ids[0] == ids[1]
        finally:
            from app.storage.redis_index import index_key
            from app.storage.redis_operation import operation_key

            keys = [
                index_key("it-gorky", "task", owner),
                operation_key("it-gorky", "task", owner, str(operation)),
            ]
            keys.extend(f"it-gorky:v1:task:{task_id}" for task_id in set(ids))
            await client.delete(*keys)
