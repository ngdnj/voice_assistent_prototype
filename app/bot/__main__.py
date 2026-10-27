"""Local run of the bot: long polling (default) or webhook, plus webhook subscription commands.

    python -m app.bot                      # long polling
    python -m app.bot webhook              # local HTTP server for MAX webhooks (behind an HTTPS tunnel)
    python -m app.bot subscribe --url https://<host>/max/webhook
    python -m app.bot unsubscribe --url https://<host>/max/webhook
    python -m app.bot subscriptions        # list subscribed URLs
    python -m app.bot doctor               # check the webhook chain step by step

This is a development entry point, not a new production process: in the target
architecture the webhook router is mounted into the API (INTEGRATION.md §3).
The backend uses the real agent and MCP/PostgreSQL. Jobs still run in process;
crash recovery requires the future durable jobs/outbox block.
"""

import argparse
import asyncio
import contextlib
import importlib.util
import logging
import os
import shutil
import signal
from collections.abc import Iterator
from datetime import UTC, datetime

from fastapi import FastAPI
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.agent.agent import Assistant, Pricing
from app.agent.bot_backend import AgentBackend
from app.agent.history import RedisChatHistory
from app.agent.mcp import McpGateway
from app.asr.base import SpeechRecognizer
from app.bot.client import MaxClient, SubscriptionError
from app.bot.gate import RedisGate
from app.bot.polling import UPDATE_TYPES, run_polling
from app.bot.service import Backend, BackgroundRunner, BotService, log_event
from app.bot.settings import BotSettings
from app.bot.webhook import build_webhook_app
from app.config import get_settings
from app.llm.model import build_chat_model, close_chat_model
from app.storage.redis import RedisSettings, redis_connection

SHUTDOWN_TIMEOUT_S = 10.0


def build_recognizer() -> SpeechRecognizer | None:
    """Our ASR if it can actually run; otherwise None (voice then uses MAX transcription or a refusal)."""
    try:
        from app.asr.factory import build_recognizer as build
        from app.config import get_settings

        settings = get_settings()
        # Both backends decode audio with ffmpeg; GigaAM also needs `uv sync --extra asr`.
        if shutil.which("ffmpeg") is None:
            log_event("asr_unavailable", backend=settings.asr_backend, reason="ffmpeg_not_found")
            return None
        if settings.asr_backend == "gigaam" and importlib.util.find_spec("gigaam") is None:
            log_event("asr_unavailable", backend="gigaam", reason="gigaam_not_installed")
            return None
        recognizer = build(settings)
    except Exception:
        # Voice then gets a polite refusal; text keeps working.
        log_event("asr_unavailable", reason="configuration_error")
        return None
    log_event("asr_ready", backend=settings.asr_backend)
    return recognizer


def build_bot(
    settings: BotSettings,
    *,
    redis: Redis,
    client: MaxClient,
    runner: BackgroundRunner,
    recognizer: SpeechRecognizer | None,
    backend: Backend,
) -> BotService:
    return BotService(
        sender=client,
        gate=RedisGate(redis, prefix=settings.key_prefix, busy_ttl_s=settings.busy_ttl_s),
        backend=backend,
        submit=runner.submit,
        audio=client,
        recognizer=recognizer,
        timezone=settings.default_timezone,
        processing_timeout_s=settings.processing_timeout_s,
        clock=lambda: datetime.now(UTC),
        allowed_chat_id=settings.test_chat_id,
    )


def install_stop(stop: asyncio.Event) -> None:
    """Ctrl+C / SIGTERM set `stop`, so both modes drain active requests and release locks."""
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows event loops have no add_signal_handler; a plain handler works there.
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop.set))


async def run_webhook_server(app: FastAPI, *, host: str, port: int, stop: asyncio.Event) -> None:
    import uvicorn

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self) -> Iterator[None]:
            # uvicorn would re-raise the signal after shutdown and kill the process before the
            # bot drains its work; install_stop handles signals instead.
            yield

    # Access logs are off: request lines are not needed and the bot logs its own events.
    server = Server(uvicorn.Config(app, host=host, port=port, log_level="warning", access_log=False))

    async def stop_on_signal() -> None:
        await stop.wait()
        server.should_exit = True

    watcher = asyncio.create_task(stop_on_signal())
    try:
        await server.serve()
    finally:
        watcher.cancel()


def start_warm_up(recognizer: SpeechRecognizer | None) -> asyncio.Task[None] | None:
    """Load a local model (GigaAM) in the background, so the first voice message is not slowed down.

    Text keeps working meanwhile; a voice message that arrives during loading waits for it.
    """
    warm_up = getattr(recognizer, "warm_up", None)
    if not callable(warm_up):
        return None

    async def run() -> None:
        log_event("asr_warm_up_started")
        try:
            await asyncio.to_thread(warm_up)
        except Exception:
            # Typically no network for the first model download; the first voice message retries it.
            log_event("asr_warm_up_failed")
        else:
            log_event("asr_warm_up_finished")

    return asyncio.create_task(run())


async def serve(mode: str, args: argparse.Namespace) -> int:
    """Run local MAX -> agent -> MCP with shared Redis and bounded shutdown."""
    settings = BotSettings.from_env()
    if mode == "webhook" and not settings.webhook_secret:
        raise ValueError("MAX_WEBHOOK_SECRET is required for the webhook mode")
    ml = get_settings()
    gateway = McpGateway(ml.mcp_url or "", os.environ.get("MCP_SERVICE_TOKEN", ""))
    model = build_chat_model(ml)
    recognizer = None if args.text_only else build_recognizer()
    client = MaxClient(settings)
    runner = BackgroundRunner(max_concurrency=settings.max_concurrency)
    warm_up = start_warm_up(recognizer)
    try:
        if mode == "polling" and await client.list_subscriptions():
            log_event("polling_blocked_by_webhook")
            return 1
        async with redis_connection(RedisSettings.from_env()) as redis:
            history = RedisChatHistory(redis, ml.history_max_turns, ml.history_ttl_s, ml.history_max_tokens)
            backend = AgentBackend(Assistant(model, gateway, history, Pricing(*ml.pricing)))
            bot = build_bot(
                settings, redis=redis, client=client, runner=runner, recognizer=recognizer, backend=backend
            )
            stop = asyncio.Event()
            install_stop(stop)
            try:
                if mode == "webhook":
                    app = build_webhook_app(bot, secret=settings.webhook_secret, path=args.path)
                    log_event("bot_webhook_started", host=args.host, port=str(args.port), path=args.path)
                    await run_webhook_server(app, host=args.host, port=args.port, stop=stop)
                else:
                    log_event("bot_polling_started", backend="agent")
                    polling = asyncio.create_task(run_polling(client, bot, stop))
                    stopping = asyncio.create_task(stop.wait())
                    try:
                        await asyncio.wait((polling, stopping), return_when=asyncio.FIRST_COMPLETED)
                        if polling.done():
                            await polling
                        else:
                            polling.cancel()
                            await asyncio.gather(polling, return_exceptions=True)
                    finally:
                        for task in (polling, stopping):
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(polling, stopping, return_exceptions=True)
            finally:
                # Drain before closing the pool used by agent state and lock release.
                await runner.aclose(SHUTDOWN_TIMEOUT_S)
    except RedisError:
        log_event("redis_unavailable")
        return 1
    finally:
        if warm_up is not None and not warm_up.done():
            # The loading thread cannot be interrupted; the process exits without waiting for it.
            warm_up.cancel()
        try:
            await client.aclose()
        finally:
            try:
                if recognizer is not None:
                    await recognizer.aclose()
            finally:
                await close_chat_model(model)
        log_event("bot_stopped")
    return 0


async def manage_subscriptions(command: str, args: argparse.Namespace) -> int:
    """subscribe / unsubscribe / subscriptions; prints only URLs and safe error codes."""
    settings = BotSettings.from_env()
    client = MaxClient(settings)
    try:
        if command == "subscribe":
            if not settings.webhook_secret:
                raise ValueError("MAX_WEBHOOK_SECRET is required to subscribe")
            await client.subscribe(url=args.url, secret=settings.webhook_secret, update_types=UPDATE_TYPES)
            print(f"Подписка создана: {args.url}")
        elif command == "unsubscribe":
            await client.unsubscribe(url=args.url)
            print(f"Подписка удалена: {args.url}")
        else:
            urls = await client.list_subscriptions()
            print("\n".join(urls) if urls else "Подписок нет: бот работает через long polling")
    except SubscriptionError as error:
        print(f"Ошибка MAX: {error.code} ({error})")
        return 1
    finally:
        await client.aclose()
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m app.bot", description="Локальный запуск MAX-бота")
    commands = parser.add_subparsers(dest="command")
    polling = commands.add_parser("polling", help="long polling (по умолчанию)")
    polling.add_argument("--text-only", action="store_true", help="do not initialize local ASR")
    webhook = commands.add_parser("webhook", help="локальный HTTP-сервер для webhook MAX")
    webhook.add_argument("--text-only", action="store_true", help="do not initialize local ASR")
    webhook.add_argument("--host", default="127.0.0.1")
    webhook.add_argument("--port", type=int, default=8080)
    webhook.add_argument("--path", default="/max/webhook")
    for name, text in (("subscribe", "подписать бота на webhook"), ("unsubscribe", "удалить подписку")):
        command = commands.add_parser(name, help=text)
        command.add_argument("--url", required=True, help="https://<host>/max/webhook")
    commands.add_parser("subscriptions", help="показать текущие подписки")
    doctor = commands.add_parser("doctor", help="проверить всю цепочку webhook по шагам")
    doctor.add_argument("--url", default=None, help="https://<host>/max/webhook (по умолчанию — из подписки)")
    doctor.add_argument("--port", type=int, default=8080, help="порт локального сервера бота")
    args = parser.parse_args(argv)
    if args.command is None:
        args.command = "polling"
        args.text_only = False
    return args


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command in ("polling", "webhook"):
        return await serve(args.command, args)
    if args.command == "doctor":
        return await diagnose(args)
    return await manage_subscriptions(args.command, args)


async def diagnose(args: argparse.Namespace) -> int:
    import httpx

    from app.bot.doctor import run_doctor

    settings = BotSettings.from_env()
    client = MaxClient(settings)
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as http:
            ok = await run_doctor(settings, url=args.url, local_port=args.port, client=client, http=http)
    finally:
        await client.aclose()
    return 0 if ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    logging.getLogger("it_gorky").setLevel(logging.INFO)
    try:
        raise SystemExit(asyncio.run(main()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None
    except Exception:
        # Validation and provider errors may contain secrets or complete requests.
        log_event("bot_start_or_runtime_failed")
        raise SystemExit(1) from None
