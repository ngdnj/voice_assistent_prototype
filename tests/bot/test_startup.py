import argparse
import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from app.bot.__main__ import parse_args, serve
from app.bot.settings import BotSettings
from app.config import Settings
from tests.agent_support import ScriptedModel


def test_text_only_is_explicit_for_polling_and_webhook():
    assert parse_args(["polling", "--text-only"]).text_only
    assert parse_args(["webhook", "--text-only"]).text_only
    assert not parse_args([]).text_only


@pytest.mark.parametrize("finish", [True, False])
async def test_stop_interrupts_polling_and_drains_before_redis_closes(finish):
    redis = AsyncMock()
    client = AsyncMock()
    client.list_subscriptions.return_value = []
    model = ScriptedModel(responses=[AIMessage("unused")])
    processing = asyncio.Event()
    ended = asyncio.Event()
    was_cancelled = False

    async def current_work():
        nonlocal was_cancelled
        processing.set()
        try:
            if finish:
                await asyncio.sleep(0.01)
            else:
                await asyncio.Event().wait()
        except asyncio.CancelledError:
            was_cancelled = True
            raise
        finally:
            redis.aclose.assert_not_awaited()
            ended.set()

    def bot_factory(*args, **kwargs):
        assert kwargs["backend"].__class__.__name__ == "AgentBackend"
        assert kwargs["runner"].submit(current_work())
        return object()

    async def polling(max_client, bot, stop):
        await processing.wait()
        stop.set()
        await asyncio.Event().wait()  # A blocked network long poll must be interrupted.

    with (
        patch("app.bot.__main__.BotSettings.from_env", return_value=BotSettings(token="test-token")),
        patch("app.bot.__main__.get_settings", return_value=Settings(mcp_url="http://127.0.0.1:8001/mcp")),
        patch("app.bot.__main__.build_chat_model", return_value=model),
        patch("app.bot.__main__.MaxClient", return_value=client),
        patch("app.storage.redis.Redis", return_value=redis),
        patch("app.bot.__main__.build_bot", side_effect=bot_factory),
        patch("app.bot.__main__.run_polling", side_effect=polling),
        patch("app.bot.__main__.install_stop"),
        patch("app.bot.__main__.SHUTDOWN_TIMEOUT_S", 0.02),
        patch.dict(os.environ, {"MCP_SERVICE_TOKEN": "test-token-" + "x" * 32}),
    ):
        assert await asyncio.wait_for(serve("polling", argparse.Namespace(text_only=True)), 2) == 0
    assert ended.is_set()
    assert was_cancelled is not finish
    redis.aclose.assert_awaited_once()
    client.aclose.assert_awaited_once()


async def test_polling_refuses_an_existing_webhook_without_deleting_it():
    client = AsyncMock()
    client.list_subscriptions.return_value = ["https://existing.example/max/webhook"]
    model = ScriptedModel(responses=[AIMessage("unused")])
    with (
        patch("app.bot.__main__.BotSettings.from_env", return_value=BotSettings(token="test-token")),
        patch("app.bot.__main__.get_settings", return_value=Settings(mcp_url="http://127.0.0.1:8001/mcp")),
        patch("app.bot.__main__.build_chat_model", return_value=model),
        patch("app.bot.__main__.MaxClient", return_value=client),
        patch("app.storage.redis.Redis") as redis_factory,
        patch.dict(os.environ, {"MCP_SERVICE_TOKEN": "test-token-" + "x" * 32}),
    ):
        assert await serve("polling", argparse.Namespace(text_only=True)) == 1
    redis_factory.assert_not_called()
    client.unsubscribe.assert_not_awaited()
    client.aclose.assert_awaited_once()
