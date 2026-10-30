"""Bot logs carry only fixed event names and correlation ids: no texts, tokens or secrets."""

import json
import logging

import pytest

from app.bot.webhook import SECRET_HEADER, handle_webhook
from tests.bot.fakes import FakeRecognizer, build_bot

SECRET = "webhook-secret-for-logs"
TOKEN = "max-token-for-logs"
PRIVATE_TEXT = "Позвонить нотариусу про завещание"
HEARD_TEXT = "напомни принять лекарство"
AUDIO_URL = "https://a.oneme.ru/voice/secret-path.ogg?sig=private-signature"


def update(mid: str, body: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "update_type": "message_created",
            "timestamp": 1,
            "message": {
                "sender": {"user_id": 42, "first_name": "Иван", "is_bot": False},
                "recipient": {"chat_id": 777},
                "body": {"mid": mid, **body},
            },
        }
    ).encode()


async def test_logs_contain_no_texts_urls_or_secrets(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    bot, parts = build_bot(recognizer=FakeRecognizer(HEARD_TEXT))
    headers = {SECRET_HEADER: SECRET}
    audio = {"attachments": [{"type": "audio", "payload": {"url": AUDIO_URL}}]}
    await handle_webhook(
        headers=headers, body=update("mid.1", {"text": PRIVATE_TEXT}), secret=SECRET, bot=bot
    )
    await parts["submit"].drain()  # type: ignore[attr-defined]
    await handle_webhook(headers=headers, body=update("mid.2", audio), secret=SECRET, bot=bot)
    await parts["submit"].drain()  # type: ignore[attr-defined]
    await handle_webhook(headers={SECRET_HEADER: TOKEN}, body=b"{}", secret=SECRET, bot=bot)
    await handle_webhook(headers=headers, body=b"not json " + PRIVATE_TEXT.encode(), secret=SECRET, bot=bot)
    assert caplog.records, "the scenario must produce log events"
    logged = "\n".join(record.getMessage() for record in caplog.records)
    for secret in (SECRET, TOKEN, PRIVATE_TEXT, HEARD_TEXT, AUDIO_URL, "secret-path", "Иван"):
        assert secret not in logged
