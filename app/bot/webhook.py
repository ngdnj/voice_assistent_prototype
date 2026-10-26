"""MAX webhook: secret check, parsing, handing the event to BotService.

MAX sends the subscription secret in `X-Max-Bot-Api-Secret` and expects HTTP 200
within 30 seconds; other codes are redelivered with growing intervals
(https://dev.max.ru/docs-api/methods/POST/subscriptions). Mounting the router into
the HTTP application belongs to role 1 (INTEGRATION.md §3).
"""

import hmac
import json
from collections.abc import Mapping
from typing import Literal

from fastapi import APIRouter, FastAPI, Request, Response

from app.bot import metrics
from app.bot.service import BotService, log_event
from app.bot.updates import InvalidUpdateError, parse_update

SECRET_HEADER = "X-Max-Bot-Api-Secret"
MAX_BODY_BYTES = 1024 * 1024

WebhookOutcome = Literal["accepted", "ignored", "unauthorized", "invalid"]


def secret_matches(headers: Mapping[str, str], secret: str) -> bool:
    received = next((value for key, value in headers.items() if key.lower() == SECRET_HEADER.lower()), "")
    return bool(secret) and hmac.compare_digest(received.encode(), secret.encode())


async def handle_webhook(
    *, headers: Mapping[str, str], body: bytes, secret: str, bot: BotService
) -> tuple[WebhookOutcome, int]:
    """Return the outcome and HTTP status for MAX. Storage errors propagate (-> 5xx, redelivery)."""
    if not secret:
        raise RuntimeError("MAX_WEBHOOK_SECRET is required for the webhook")
    if not secret_matches(headers, secret):
        metrics.UPDATES.labels("other", "unauthorized").inc()
        log_event("webhook_unauthorized")
        return "unauthorized", 401
    try:
        if len(body) > MAX_BODY_BYTES:
            raise InvalidUpdateError("Body is too large")
        event = parse_update(json.loads(body))
    # The secret proves the sender is MAX: a schema mismatch is our bug, and a non-200 answer
    # would only make MAX redeliver it for 8 hours and then drop the subscription.
    except InvalidUpdateError as error:
        metrics.UPDATES.labels("other", "invalid").inc()
        log_event("webhook_invalid", reason=str(error))
        return "invalid", 200
    except ValueError:
        metrics.UPDATES.labels("other", "invalid").inc()
        log_event("webhook_invalid", reason="invalid json")
        return "invalid", 200
    outcome = await bot.handle(event)
    # One line per delivery: shows that MAX reaches the server and what the bot decided.
    kind = type(event).__name__
    log_event("webhook_received", kind=kind, outcome=outcome)
    return ("ignored" if outcome == "ignored" else "accepted"), 200


def webhook_router(bot: BotService, *, secret: str, path: str = "/max/webhook") -> APIRouter:
    """FastAPI router for the webhook; the caller owns the application and the bot lifecycle."""
    if not secret:
        raise ValueError("MAX_WEBHOOK_SECRET is required for the webhook")
    router = APIRouter()

    @router.post(path, include_in_schema=False)
    async def max_webhook(request: Request) -> Response:
        outcome, status = await handle_webhook(
            headers=dict(request.headers), body=await request.body(), secret=secret, bot=bot
        )
        return Response(
            content=json.dumps({"outcome": outcome}), status_code=status, media_type="application/json"
        )

    return router


def build_webhook_app(bot: BotService, *, secret: str, path: str = "/max/webhook") -> FastAPI:
    """Minimal app for the local webhook mode (`python -m app.bot webhook`), not the target API."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.include_router(webhook_router(bot, secret=secret, path=path))

    @app.get("/health", include_in_schema=False)
    async def health() -> dict[str, str]:
        return {"status": "ok"}  # Liveness only: no calls to MAX, Redis or models.

    return app
