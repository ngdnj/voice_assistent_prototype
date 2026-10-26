import json

import httpx
import pytest

from app.bot import client as client_module
from app.bot.client import (
    AudioFetchError,
    AudioSourceError,
    AudioTooLargeError,
    MaxClient,
    PollingError,
    SubscriptionError,
    split_text,
    validate_webhook_url,
)
from app.bot.settings import BotSettings

TOKEN = "test-token-not-real"


def settings(**overrides: object) -> BotSettings:
    values: dict[str, object] = {"token": TOKEN, "audio_max_bytes": 100}
    values.update(overrides)
    return BotSettings(**values)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "RETRY_DELAY_S", 0.0)


async def test_send_uses_header_token_and_chat_id() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"message": {"body": {"mid": "mid.5", "text": "ok"}}})

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    result = await max_client.send_text(chat_id="777", text="Привет")
    await max_client.aclose()
    assert result.ok and result.external_message_id == "mid.5"
    request = seen[0]
    assert request.url.host == "platform-api2.max.ru" and request.url.path == "/messages"
    assert request.url.params["chat_id"] == "777"
    assert "access_token" not in str(request.url)
    assert request.headers["Authorization"] == TOKEN
    assert json.loads(request.content) == {"text": "Привет"}


@pytest.mark.parametrize(
    ("responses", "outcome", "calls"),
    [
        ([429, 200], "delivered", 2),
        ([500, 502, 503], "retryable_failure", 3),
        ([400], "permanent_failure", 1),
        ([401], "permanent_failure", 1),
    ],
)
async def test_send_retry_policy(responses: list[int], outcome: str, calls: int) -> None:
    codes = iter(responses)
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        return httpx.Response(next(codes), json={})

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    result = await max_client.send_text(chat_id="1", text="x")
    await max_client.aclose()
    assert (result.outcome, count) == (outcome, calls)
    assert TOKEN not in result.error_code


async def test_timeout_is_unknown_and_not_repeated() -> None:
    count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal count
        count += 1
        raise httpx.ReadTimeout("slow", request=request)

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    result = await max_client.send_text(chat_id="1", text="x")
    await max_client.aclose()
    assert (result.outcome, count) == ("unknown", 1)


async def test_long_text_is_split() -> None:
    texts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        texts.append(json.loads(request.content)["text"])
        return httpx.Response(200, json={})

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    assert (await max_client.send_text(chat_id="1", text=("слово " * 1500))).ok
    await max_client.aclose()
    assert len(texts) == 3 and all(len(text) <= 4000 for text in texts)


def test_split_text_keeps_content() -> None:
    text = "a" * 9000
    parts = split_text(text)
    assert "".join(parts) == text and max(map(len, parts)) == 4000


async def test_download_audio_ok_without_token() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=b"audio")

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    assert await max_client.download_audio("https://media.max.ru/a.ogg") == b"audio"
    await max_client.aclose()
    assert "Authorization" not in seen[0].headers


@pytest.mark.parametrize(
    "url",
    [
        "http://media.max.ru/a.ogg",
        "https://evil.example/a.ogg",
        "https://max.ru.evil.example/a.ogg",
        "https://127.0.0.1/a.ogg",
        "https://user:pw@media.max.ru/a.ogg",
        "file:///etc/passwd",
    ],
)
async def test_download_audio_rejects_foreign_sources(url: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not be requested")

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(AudioSourceError):
        await max_client.download_audio(url)
    await max_client.aclose()


async def test_redirect_to_foreign_host_is_rejected() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "media.max.ru":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest"})
        raise AssertionError("must not follow")

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(AudioSourceError) as caught:
        await max_client.download_audio("https://media.max.ru/a.ogg")
    await max_client.aclose()
    assert caught.value.host == "169.254.169.254"


async def test_redirect_inside_max_is_followed() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/a.ogg":
            return httpx.Response(302, headers={"location": "https://cdn.max.ru/b.ogg"})
        return httpx.Response(200, content=b"audio")

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    assert await max_client.download_audio("https://media.max.ru/a.ogg") == b"audio"
    await max_client.aclose()


@pytest.mark.parametrize(
    ("response", "error"),
    [
        (httpx.Response(200, content=b"x" * 101), AudioTooLargeError),
        (httpx.Response(200, headers={"content-length": "1000"}, content=b"x" * 1000), AudioTooLargeError),
        (httpx.Response(404), AudioFetchError),
        (httpx.Response(200, content=b""), AudioFetchError),
    ],
)
async def test_download_audio_limits(response: httpx.Response, error: type[Exception]) -> None:
    max_client = MaxClient(settings(), transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(error):
        await max_client.download_audio("https://media.max.ru/a.ogg")
    await max_client.aclose()


async def test_get_updates() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["marker"] == "10"
        assert request.url.params["types"] == "bot_started,message_created"
        return httpx.Response(200, json={"updates": [{"update_type": "bot_started"}], "marker": 11})

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    updates, marker = await max_client.get_updates(
        marker=10, timeout_s=1, types=("bot_started", "message_created")
    )
    await max_client.aclose()
    assert updates == [{"update_type": "bot_started"}] and marker == 11


def test_settings_validation() -> None:
    assert TOKEN not in repr(settings())
    for overrides in (
        {"token": " "},
        {"api_url": "http://platform-api2.max.ru"},
        {"webhook_secret": "abc"},
        {"default_timezone": "Mars/Base"},
        {"audio_hosts": (" ",)},
        {"send_timeout_s": 25.0},
        {"busy_ttl_s": 60},
        {"max_concurrency": 0},
    ):
        with pytest.raises(Exception):  # noqa: B017 - ValueError or ZoneInfoNotFoundError
            settings(**overrides)


@pytest.mark.parametrize(
    ("error", "code"),
    [
        (httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"), "tls_certificate"),
        (httpx.ConnectError("Name or service not known"), "connect_error"),
        (httpx.ReadTimeout("slow"), "timeout"),
        (httpx.ProxyError("403"), "proxy_error"),
    ],
)
async def test_polling_error_codes(error: httpx.TransportError, code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise error

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(PollingError) as caught:
        await max_client.get_updates(marker=None, timeout_s=1)
    await max_client.aclose()
    assert caught.value.code == code


@pytest.mark.parametrize(("status", "code"), [(401, "http_401"), (400, "http_400")])
async def test_polling_http_error_code(status: int, code: str) -> None:
    max_client = MaxClient(settings(), transport=httpx.MockTransport(lambda request: httpx.Response(status)))
    with pytest.raises(PollingError) as caught:
        await max_client.get_updates(marker=None, timeout_s=1)
    await max_client.aclose()
    assert caught.value.code == code
    assert TOKEN not in str(caught.value)


def test_ca_bundle_must_exist() -> None:
    with pytest.raises(ValueError):
        settings(ca_bundle_file="missing.pem")


async def test_subscribe_sends_url_types_and_secret() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"success": True})

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    await max_client.subscribe(
        url="https://bot.example.ru/max/webhook", secret="secret-1", update_types=("bot_started",)
    )
    await max_client.aclose()
    request = seen[0]
    assert (request.method, request.url.path) == ("POST", "/subscriptions")
    assert json.loads(request.content) == {
        "url": "https://bot.example.ru/max/webhook",
        "update_types": ["bot_started"],
        "secret": "secret-1",
    }


async def test_unsubscribe_and_list() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "DELETE":
            assert request.url.params["url"] == "https://bot.example.ru/max/webhook"
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200, json={"subscriptions": [{"url": "https://bot.example.ru/max/webhook", "time": 1}]}
        )

    max_client = MaxClient(settings(), transport=httpx.MockTransport(handler))
    await max_client.unsubscribe(url="https://bot.example.ru/max/webhook")
    assert await max_client.list_subscriptions() == ["https://bot.example.ru/max/webhook"]
    await max_client.aclose()


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(200, json={"success": False, "message": "url is unreachable"}), "rejected"),
        (httpx.Response(401), "http_401"),
    ],
)
async def test_subscription_errors(response: httpx.Response, code: str) -> None:
    max_client = MaxClient(settings(), transport=httpx.MockTransport(lambda request: response))
    with pytest.raises(SubscriptionError) as caught:
        await max_client.unsubscribe(url="https://bot.example.ru/x")
    await max_client.aclose()
    assert caught.value.code == code
    assert TOKEN not in str(caught.value)


@pytest.mark.parametrize(
    "url",
    [
        "http://bot.example.ru/max/webhook",
        "https://bot.example.ru:8443/max/webhook",
        "https://user:pw@bot.example.ru/max/webhook",
        "https://bot.example.ru/max/webhook?x=1",
        "bot.example.ru/max/webhook",
    ],
)
def test_webhook_url_validation(url: str) -> None:
    with pytest.raises(ValueError):
        validate_webhook_url(url)
    assert validate_webhook_url("https://abc.trycloudflare.com/max/webhook")


def test_webhook_secret_format() -> None:
    assert settings(webhook_secret="Abc_12-xyz").webhook_secret == "Abc_12-xyz"
    for secret in ("abc", "with space", "кириллица1", "a" * 257):
        with pytest.raises(ValueError):
            settings(webhook_secret=secret)
