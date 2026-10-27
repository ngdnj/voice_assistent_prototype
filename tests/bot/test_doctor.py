import json

import httpx

from app.bot.client import MaxClient
from app.bot.doctor import Step, run_doctor
from app.bot.settings import BotSettings

URL = "https://abc.trycloudflare.com/max/webhook"
SECRET = "doctor-secret-1"


def max_client(subscriptions: list[str], me_status: int = 200) -> MaxClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/me":
            return httpx.Response(me_status, json={})
        return httpx.Response(200, json={"subscriptions": [{"url": url} for url in subscriptions]})

    settings = BotSettings(token="t-not-real", webhook_secret=SECRET)
    return MaxClient(settings, transport=httpx.MockTransport(handler))


def network(*, local: int = 200, tunnel: int = 200, secret_ok: bool = True) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "127.0.0.1":
            return httpx.Response(local, json={"status": "ok"})
        if request.method == "GET":
            return httpx.Response(tunnel, json={"status": "ok"})
        assert json.loads(request.content) == {"update_type": "dialog_muted", "timestamp": 1}
        if request.headers.get("X-Max-Bot-Api-Secret") != SECRET or not secret_ok:
            return httpx.Response(401, json={"outcome": "unauthorized"})
        return httpx.Response(200, json={"outcome": "ignored"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


async def redis_ok() -> Step:
    return Step("Redis", True)


async def run(subscriptions: list[str], **kwargs: object) -> tuple[bool, list[str]]:
    lines: list[str] = []
    client = max_client(subscriptions)
    async with network(**kwargs) as http:  # type: ignore[arg-type]
        ok = await run_doctor(
            BotSettings(token="t-not-real", webhook_secret=SECRET),
            url=None,
            local_port=8080,
            client=client,
            http=http,
            redis_step=redis_ok,
            out=lines.append,
        )
    await client.aclose()
    return ok, lines


async def test_whole_chain_ok() -> None:
    ok, lines = await run([URL])
    assert ok
    assert all(not line.startswith("[ERR]") for line in lines)
    assert not any("t-not-real" in line or SECRET in line for line in lines)


async def test_stale_subscription_is_the_first_problem() -> None:
    ok, lines = await run(["https://old.trycloudflare.com/max/webhook", URL])
    assert not ok
    assert lines[-1] == "Первая проблема: Подписка MAX указывает ровно на адрес туннеля"


async def test_bot_not_running() -> None:
    ok, lines = await run([URL], local=502)
    assert not ok
    assert any(line.startswith("[ERR] Локальный сервер") for line in lines)


async def test_wrong_secret_on_server() -> None:
    ok, lines = await run([URL], secret_ok=False)
    assert not ok
    assert any("перезапустите бота" in line for line in lines)
