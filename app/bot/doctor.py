"""`python -m app.bot doctor`: checks the whole webhook chain step by step and names the broken link.

MAX -> subscription -> HTTPS tunnel -> local server -> bot. Prints only step names, short codes
and URLs; never the token, the secret or message contents.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx
from redis.exceptions import RedisError

from app.bot.client import MaxClient, SubscriptionError
from app.bot.settings import BotSettings
from app.bot.webhook import SECRET_HEADER
from app.storage.redis import RedisSettings, redis_connection

# A harmless update type: the bot ignores it and sends nothing to anyone.
PROBE_UPDATE = {"update_type": "dialog_muted", "timestamp": 1}


@dataclass(frozen=True)
class Step:
    name: str
    ok: bool
    detail: str = ""
    hint: str = ""


Print = Callable[[str], None]


async def _redis_step() -> Step:
    try:
        async with redis_connection(RedisSettings.from_env()):
            return Step("Redis", True)
    except (RedisError, ValueError):
        return Step("Redis", False, hint="Запустите Docker Desktop и `docker compose up -d --wait redis`")


async def _get(http: httpx.AsyncClient, url: str) -> tuple[int, str]:
    try:
        response = await http.get(url)
    except httpx.HTTPError as error:
        return 0, type(error).__name__
    return response.status_code, response.text[:100]


async def _check_tunnel(
    settings: BotSettings, url: str, http: httpx.AsyncClient, report: Callable[[Step], bool]
) -> None:
    parts = urlsplit(url)
    base = f"{parts.scheme}://{parts.netloc}"
    status, _ = await _get(http, f"{base}/health")
    report(
        Step(
            f"Туннель ({base}/health)",
            status == 200,
            f"HTTP {status}" if status else "нет ответа",
            "Окно cloudflared должно быть открыто; адрес меняется при каждом его запуске",
        )
    )

    try:
        response = await http.post(url, json=PROBE_UPDATE, headers={SECRET_HEADER: settings.webhook_secret})
        probe_status, body = response.status_code, response.text
    except httpx.HTTPError as error:
        probe_status, body = 0, type(error).__name__
    hints = {
        401: "Секрет бота не совпадает: перезапустите бота после правки .env",
        404: "Путь не /max/webhook или на порту не бот",
    }
    report(
        Step(
            "Пробное событие через туннель до бота",
            probe_status == 200 and "ignored" in body,
            f"HTTP {probe_status}" if probe_status else body,
            hints.get(probe_status, "Смотрите окно 1"),
        )
    )


async def run_doctor(
    settings: BotSettings,
    *,
    url: str | None,
    local_port: int,
    client: MaxClient,
    http: httpx.AsyncClient,
    redis_step: Callable[[], Awaitable[Step]] = _redis_step,
    out: Print = print,
) -> bool:
    steps: list[Step] = []

    def report(step: Step) -> bool:
        steps.append(step)
        mark = "OK " if step.ok else "ERR"
        out(f"[{mark}] {step.name}" + (f": {step.detail}" if step.detail else ""))
        if not step.ok and step.hint:
            out(f"      -> {step.hint}")
        return step.ok

    report(Step("MAX_WEBHOOK_SECRET задан", bool(settings.webhook_secret), hint="Впишите секрет в .env"))
    report(await redis_step())

    code = await client.check_token()
    report(
        Step(
            "Токен MAX (GET /me)",
            not code,
            code,
            "Проверьте MAX_BOT_TOKEN; при tls_certificate — MAX_CA_BUNDLE_FILE; отключите VPN",
        )
    )

    try:
        subscriptions = await client.list_subscriptions()
    except SubscriptionError as error:
        report(Step("Подписки MAX", False, error.code))
        subscriptions = []
    else:
        detail = ", ".join(subscriptions) if subscriptions else "нет подписок"
        if url is None and len(subscriptions) == 1:
            url = subscriptions[0]
        expected_ok = url is not None and subscriptions == [url]
        report(
            Step(
                "Подписка MAX указывает ровно на адрес туннеля",
                expected_ok,
                detail,
                "Удалите лишние/старые адреса (unsubscribe) и выполните subscribe с текущим адресом туннеля",
            )
        )

    status, _ = await _get(http, f"http://127.0.0.1:{local_port}/health")
    report(
        Step(
            f"Локальный сервер бота (127.0.0.1:{local_port}/health)",
            status == 200,
            f"HTTP {status}" if status else "нет ответа",
            "В окне 1 должен работать `python -m app.bot webhook` (строка bot_webhook_started)",
        )
    )

    if url is None:
        report(Step("Туннель", False, "адрес неизвестен", "Передайте --url https://<адрес>/max/webhook"))
    else:
        await _check_tunnel(settings, url, http, report)

    failed = [step for step in steps if not step.ok]
    if failed:
        out(f"Первая проблема: {failed[0].name}")
    else:
        out(
            "Вся цепочка до бота работает. Если бот всё равно молчит, MAX не доставляет события "
            "на этот адрес: попробуйте отключить VPN или другой туннель (tuna.am), затем subscribe заново."
        )
    return not failed
