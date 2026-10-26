import json

import httpx
import pytest
from fastapi import FastAPI

from app.bot import messages
from app.bot.webhook import SECRET_HEADER, build_webhook_app, handle_webhook, webhook_router
from tests.bot.fakes import FakeGate, build_bot

SECRET = "webhook-secret-1"
TEXT_UPDATE = {
    "update_type": "message_created",
    "timestamp": 1,
    "message": {
        "sender": {"user_id": 42, "is_bot": False},
        "recipient": {"chat_id": 777},
        "body": {"mid": "mid.1", "text": "Создай задачу"},
    },
}


async def test_valid_update_is_accepted() -> None:
    bot, parts = build_bot()
    outcome = await handle_webhook(
        headers={SECRET_HEADER.lower(): SECRET}, body=json.dumps(TEXT_UPDATE).encode(), secret=SECRET, bot=bot
    )
    assert outcome == ("accepted", 200)
    await parts["submit"].drain()  # type: ignore[attr-defined]


@pytest.mark.parametrize("headers", [{}, {SECRET_HEADER: "wrong-secret"}])
async def test_wrong_secret_is_rejected_before_parsing(headers: dict[str, str]) -> None:
    bot, parts = build_bot()
    outcome = await handle_webhook(headers=headers, body=b"not json", secret=SECRET, bot=bot)
    assert outcome == ("unauthorized", 401)
    assert not parts["gate"].events  # type: ignore[attr-defined]


@pytest.mark.parametrize("body", [b"not json", b"[]", json.dumps({"timestamp": 1}).encode()])
async def test_invalid_body(body: bytes) -> None:
    bot, _ = build_bot()
    assert await handle_webhook(headers={SECRET_HEADER: SECRET}, body=body, secret=SECRET, bot=bot) == (
        "invalid",
        200,
    )


async def test_unknown_update_type_is_ignored() -> None:
    bot, _ = build_bot()
    body = json.dumps({"update_type": "dialog_muted", "timestamp": 1}).encode()
    assert await handle_webhook(headers={SECRET_HEADER: SECRET}, body=body, secret=SECRET, bot=bot) == (
        "ignored",
        200,
    )


async def test_router_and_storage_error_is_5xx() -> None:
    gate = FakeGate()
    bot, parts = build_bot(gate=gate)
    app = FastAPI()
    app.include_router(webhook_router(bot, secret=SECRET))
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        started = {"update_type": "bot_started", "timestamp": 2, "chat_id": 777, "user": {"user_id": 42}}
        response = await http.post("/max/webhook", json=started, headers={SECRET_HEADER: SECRET})
        assert response.status_code == 200
        assert parts["sender"].texts == [messages.WELCOME]  # type: ignore[attr-defined]
        gate.fail = True
        response = await http.post("/max/webhook", json=TEXT_UPDATE, headers={SECRET_HEADER: SECRET})
        assert response.status_code == 500  # MAX redelivers the event later.


def test_router_requires_secret() -> None:
    bot, _ = build_bot()
    with pytest.raises(ValueError):
        webhook_router(bot, secret="")


async def test_local_webhook_app_health_and_route() -> None:
    bot, parts = build_bot()
    app = build_webhook_app(bot, secret=SECRET)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
        assert (await http.get("/health")).json() == {"status": "ok"}
        assert (await http.get("/docs")).status_code == 404
        response = await http.post("/max/webhook", json=TEXT_UPDATE, headers={SECRET_HEADER: SECRET})
        assert response.status_code == 200
        await parts["submit"].drain()  # type: ignore[attr-defined]
    assert parts["sender"].texts[0] == messages.ACCEPTED  # type: ignore[attr-defined]


def test_cli_arguments() -> None:
    from app.bot.__main__ import parse_args

    assert parse_args([]).command == "polling"
    webhook = parse_args(["webhook", "--port", "9000"])
    assert (webhook.command, webhook.host, webhook.port, webhook.path) == (
        "webhook",
        "127.0.0.1",
        9000,
        "/max/webhook",
    )
    assert parse_args(["subscribe", "--url", "https://x.ru/max/webhook"]).url == "https://x.ru/max/webhook"
