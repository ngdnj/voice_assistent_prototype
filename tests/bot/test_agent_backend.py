from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.agent.agent import AgentReply, Assistant
from app.agent.bot_backend import AgentBackend
from app.bot.service import BackendRequest
from app.bot.settings import BotSettings
from app.bot.updates import parse_update
from tests.bot.fakes import build_bot


async def test_backend_preserves_trusted_context_and_job_identity():
    assistant = AsyncMock(spec=Assistant)
    assistant.handle.return_value = AgentReply("Сохранено.")
    backend = AgentBackend(assistant)
    request = BackendRequest(
        request_id=uuid4(),
        job_id=str(uuid4()),
        owner_id="42",
        chat_id="777",
        text="Создай задачу",
        reference_time=datetime(2026, 10, 26, 8, tzinfo=UTC),
        timezone="Europe/Moscow",
    )
    assert await backend.answer(request) == "Сохранено."
    assert await backend.answer(request) == "Сохранено."
    for call in assistant.handle.await_args_list:
        assert call.args == (request.owner_id, request.text)
        assert call.kwargs == {
            "chat_id": request.chat_id,
            "reference_time": request.reference_time,
            "timezone": request.timezone,
            "operation_id": request.job_id,
            "request_id": str(request.request_id),
            "confirmation_operation_id": None,
        }


async def test_backend_does_not_turn_storage_failure_into_success():
    assistant = AsyncMock(spec=Assistant)
    assistant.handle.side_effect = ConnectionError("storage unavailable")
    request = BackendRequest(
        request_id=uuid4(),
        job_id=str(uuid4()),
        owner_id="42",
        chat_id="777",
        text="Запрос",
        reference_time=datetime(2026, 10, 26, 8, tzinfo=UTC),
        timezone="Europe/Moscow",
    )
    with pytest.raises(ConnectionError):
        await AgentBackend(assistant).answer(request)


async def test_local_chat_restriction_prevents_all_sends_and_admission():
    bot, parts = build_bot(allowed_chat_id="999")
    event = parse_update(
        {"update_type": "bot_started", "timestamp": 1, "chat_id": 777, "user": {"user_id": 42}}
    )
    assert await bot.handle(event) == "ignored"
    assert parts["sender"].sent == []
    assert parts["gate"].events == set()


def test_chat_restriction_is_optional_and_validated():
    assert BotSettings(token="test").test_chat_id is None
    assert BotSettings(token="test", test_chat_id="777").test_chat_id == "777"
    with pytest.raises(ValueError, match="MAX_TEST_CHAT_ID"):
        BotSettings(token="test", test_chat_id="wrong")
