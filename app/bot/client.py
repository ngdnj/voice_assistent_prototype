"""Thin MAX Bot API client: send text, download audio, long polling. No business logic.

Docs: https://dev.max.ru/docs-api/methods/POST/messages. The token goes in the
`Authorization` header without the `Bearer` prefix; query tokens are not supported.
Errors never carry the token, URLs with credentials or response bodies.
"""

import asyncio
import ipaddress
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urljoin, urlsplit

import certifi
import httpx

from app.bot.settings import MAX_TEXT_LENGTH, SEND_ATTEMPTS, BotSettings

SendOutcome = Literal["delivered", "retryable_failure", "permanent_failure", "unknown"]
MAX_REDIRECTS = 3
# First retry delay; the documented limit is 2 messages per second per dialog.
RETRY_DELAY_S = 1.0
# Spacing of sends to one recipient, so a status, a reply and a reminder stay within that limit.
MIN_SEND_INTERVAL_S = 0.5
# Forget pacing slots of idle recipients once there are this many.
PACING_PRUNE_AT = 1000


@dataclass(frozen=True)
class AppButton:
    """Inline button of type `open_app`: opens the Mini App of the bot named by `web_app`.

    https://dev.max.ru/docs-api/methods/POST/messages: `web_app` is the bot's public name or link.
    """

    text: str
    web_app: str


# A callback button is (text, payload); an AppButton opens the Mini App.
Button = tuple[str, str] | AppButton


@dataclass(frozen=True)
class SendResult:
    outcome: SendOutcome
    external_message_id: str = ""
    error_code: str = ""

    @property
    def ok(self) -> bool:
        return self.outcome == "delivered"


class AudioSourceError(ValueError):
    """The audio URL is not an allowed HTTPS host of MAX; not retried."""

    def __init__(self, message: str = "Audio source is not allowed", *, host: str = "") -> None:
        super().__init__(message)
        self.host = host  # Only the host name, for diagnostics: never the full URL with its token.


class AudioTooLargeError(ValueError):
    """The audio is larger than MAX_AUDIO_MAX_BYTES; not retried."""


class AudioFetchError(RuntimeError):
    """Network or HTTP failure while downloading audio; the stage may be retried."""


class PollingError(RuntimeError):
    """GET /updates failed; `code` is a short safe reason for logs."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def transport_error_code(error: httpx.TransportError) -> str:
    """Safe short reason of a network failure, without URLs, headers or the token."""
    if isinstance(error, httpx.TimeoutException):
        return "timeout"
    if isinstance(error, httpx.ProxyError):
        return "proxy_error"
    text = str(error).lower()
    if "certificate" in text or "ssl" in text:
        return "tls_certificate"  # Usually the Russian Trusted Root CA is missing: MAX_CA_BUNDLE_FILE.
    if isinstance(error, httpx.ConnectError):
        return "connect_error"
    return "network_error"


def build_ssl_context(ca_bundle_file: str | None) -> ssl.SSLContext:
    """Public CAs plus an optional extra PEM bundle (for example, the Russian Trusted Root CA)."""
    context = ssl.create_default_context(cafile=certifi.where())
    if ca_bundle_file:
        context.load_verify_locations(cafile=ca_bundle_file)
    return context


def split_text(text: str, limit: int = MAX_TEXT_LENGTH) -> list[str]:
    """Split a long reply on line or word boundaries into parts the API accepts."""
    parts: list[str] = []
    rest = text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit + 1)
        if cut <= 0:
            cut = rest.rfind(" ", 0, limit + 1)
        if cut <= 0:
            cut = limit
        parts.append(rest[:cut].rstrip())
        rest = rest[cut:].lstrip()
    if rest.strip():
        parts.append(rest)
    return parts


def _button(button: Button) -> dict[str, str]:
    if isinstance(button, AppButton):
        return {"type": "open_app", "text": button.text, "web_app": button.web_app}
    label, payload = button
    return {"type": "callback", "text": label, "payload": payload}


def _message_body(text: str, buttons: tuple[Button, ...]) -> dict[str, object]:
    body: dict[str, object] = {"text": text}
    if buttons:
        row = [_button(button) for button in buttons]
        body["attachments"] = [{"type": "inline_keyboard", "payload": {"buttons": [row]}}]
    return body


def _message_id(payload: object) -> str:
    if isinstance(payload, dict):
        message = payload.get("message")
        if isinstance(message, dict):
            body = message.get("body")
            if isinstance(body, dict) and isinstance(body.get("mid"), str):
                return str(body["mid"])
    return ""


def _host_allowed(url: str, hosts: tuple[str, ...]) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or not host or parts.username or parts.password:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # Literal IPs are never a MAX media host and may point inside the network.
    except ValueError:
        pass
    return any(host == allowed or host.endswith("." + allowed) for allowed in hosts)


class SubscriptionError(RuntimeError):
    """A /subscriptions call failed; `code` is a short safe reason for logs and the CLI."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code


def validate_webhook_url(url: str) -> str:
    """MAX accepts only https URLs on port 443, without an explicit port."""
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname or parts.port is not None:
        raise ValueError("Webhook URL must be https://<host>/<path> without a port")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Webhook URL must not contain credentials, query or fragment")
    return url


class MaxClient:
    def __init__(self, settings: BotSettings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._settings = settings
        verify = build_ssl_context(settings.ca_bundle_file)
        self._http = httpx.AsyncClient(
            base_url=settings.api_url,
            headers={"Authorization": settings.token},
            timeout=httpx.Timeout(settings.send_timeout_s),
            follow_redirects=False,
            transport=transport,
            verify=verify,
        )
        # Next allowed send time per recipient, in event-loop time; a lock keeps slots unique.
        self._next_send: dict[tuple[str, str], float] = {}
        self._pacing = asyncio.Lock()
        # Audio is downloaded without the bot token: media URLs are not API calls.
        self._media = httpx.AsyncClient(
            timeout=httpx.Timeout(settings.send_timeout_s),
            follow_redirects=False,
            transport=transport,
            verify=verify,
        )

    async def aclose(self) -> None:
        await self._http.aclose()
        await self._media.aclose()

    async def send_text(self, *, chat_id: str, text: str, buttons: tuple[Button, ...] = ()) -> SendResult:
        """Send one message (split if needed). Retries 429/5xx; a timeout is `unknown`, not failure.

        `buttons` (callback or Mini App) form one row attached to the last part.
        """
        if not text.strip():
            return SendResult("permanent_failure", error_code="empty_text")
        result = SendResult("permanent_failure", error_code="empty_text")
        parts = split_text(text)
        for index, part in enumerate(parts):
            last = index == len(parts) - 1
            result = await self._send_part(
                params={"chat_id": chat_id}, text=part, buttons=buttons if last else ()
            )
            if not result.ok:
                return result
        return result

    async def send_to_user(self, *, user_id: str, text: str) -> SendResult:
        """Send to a user's dialog by MAX user_id (POST /messages?user_id=), e.g. a reminder.

        Used when no incoming message gave a chat_id. Same retries and outcomes as `send_text`.
        """
        if not text.strip():
            return SendResult("permanent_failure", error_code="empty_text")
        result = SendResult("permanent_failure", error_code="empty_text")
        for part in split_text(text):
            result = await self._send_part(params={"user_id": user_id}, text=part)
            if not result.ok:
                return result
        return result

    async def delete_message(self, *, message_id: str) -> str:
        """DELETE /messages: "" on success, else a short safe code. Bots delete only own messages."""
        return await self._simple_call("DELETE", "/messages", params={"message_id": message_id})

    async def answer_callback(self, *, callback_id: str) -> str:
        """POST /answers without a body: acknowledges the button press and changes nothing."""
        return await self._simple_call("POST", "/answers", params={"callback_id": callback_id}, json={})

    async def _simple_call(
        self, method: str, path: str, *, params: dict[str, str], json: object = None
    ) -> str:
        try:
            response = await self._http.request(method, path, params=params, json=json)
        except httpx.TransportError as error:
            return transport_error_code(error)
        if response.status_code != 200:
            return f"http_{response.status_code}"
        try:
            payload = response.json()
        except ValueError:
            return "invalid_json"
        # MAX answers 200 with success=false and a reason for refused operations.
        if isinstance(payload, dict) and payload.get("success") is False:
            return "rejected"
        return ""

    async def _pace(self, recipient: tuple[str, str]) -> None:
        """Wait for this recipient's next send slot (MAX: 2 messages per second per dialog)."""
        loop = asyncio.get_running_loop()
        async with self._pacing:
            now = loop.time()
            if len(self._next_send) >= PACING_PRUNE_AT:
                self._next_send = {key: at for key, at in self._next_send.items() if at > now}
            slot = max(now, self._next_send.get(recipient, now))
            self._next_send[recipient] = slot + MIN_SEND_INTERVAL_S
        if slot > now:
            await asyncio.sleep(slot - now)

    async def _send_part(
        self, *, params: Mapping[str, str], text: str, buttons: tuple[Button, ...] = ()
    ) -> SendResult:
        delay = RETRY_DELAY_S
        result = SendResult("retryable_failure", error_code="not_sent")
        recipient = next(iter(params.items()))
        for attempt in range(SEND_ATTEMPTS):
            await self._pace(recipient)
            try:
                response = await self._http.post(
                    "/messages", params=dict(params), json=_message_body(text, buttons)
                )
            except httpx.TimeoutException:
                # The request may have reached MAX: repeating it risks a duplicate message.
                return SendResult("unknown", error_code="timeout")
            except httpx.TransportError as error:
                result = SendResult("retryable_failure", error_code=transport_error_code(error))
            else:
                if response.status_code == 200:
                    try:
                        payload: object = response.json()
                    except ValueError:
                        payload = None
                    return SendResult("delivered", external_message_id=_message_id(payload))
                if response.status_code == 429 or response.status_code >= 500:
                    result = SendResult("retryable_failure", error_code=f"http_{response.status_code}")
                else:
                    return SendResult("permanent_failure", error_code=f"http_{response.status_code}")
            if attempt + 1 < SEND_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= 2
        return result

    async def edit_text(self, *, message_id: str, text: str) -> SendResult:
        """PUT /messages returns {success: bool}, not a Message. Replace a fresh status.

        Repeating a PUT to the same ID with the same text is safe. A reply longer than
        the API limit falls back to split sends at the service boundary.
        """
        if not message_id or not text.strip() or len(text) > MAX_TEXT_LENGTH:
            return SendResult("permanent_failure", error_code="invalid_edit")
        delay = RETRY_DELAY_S
        result = SendResult("retryable_failure", error_code="not_edited")
        for attempt in range(SEND_ATTEMPTS):
            try:
                response = await self._http.put(
                    "/messages", params={"message_id": message_id}, json={"text": text}
                )
            except httpx.TransportError as error:
                result = SendResult("retryable_failure", error_code=transport_error_code(error))
            else:
                if response.status_code == 200:
                    try:
                        payload = response.json()
                    except ValueError:
                        return SendResult("unknown", error_code="invalid_response")
                    if isinstance(payload, dict) and payload.get("success") is True:
                        return SendResult("delivered", external_message_id=message_id)
                    return SendResult("permanent_failure", error_code="edit_rejected")
                if response.status_code == 429 or response.status_code >= 500:
                    result = SendResult("retryable_failure", error_code=f"http_{response.status_code}")
                else:
                    return SendResult("permanent_failure", error_code=f"http_{response.status_code}")
            if attempt + 1 < SEND_ATTEMPTS:
                await asyncio.sleep(delay)
                delay *= 2
        return result

    async def download_audio(self, url: str) -> bytes:
        """Stream audio from an allowed HTTPS host with a size limit; redirects are re-checked."""
        hosts = self._settings.audio_hosts
        limit = self._settings.audio_max_bytes
        for _ in range(MAX_REDIRECTS + 1):
            if not _host_allowed(url, hosts):
                host = (urlsplit(url).hostname or "")[:100]
                raise AudioSourceError("Audio host is not allowed", host=host)
            try:
                async with self._media.stream("GET", url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        url = urljoin(url, location)
                        continue
                    if response.status_code != 200:
                        raise AudioFetchError(f"Audio download failed: HTTP {response.status_code}")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > limit:
                        raise AudioTooLargeError("Audio is too large")
                    chunks: list[bytes] = []
                    size = 0
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise AudioTooLargeError("Audio is too large")
                        chunks.append(chunk)
                    if not size:
                        raise AudioFetchError("Audio download returned no data")
                    return b"".join(chunks)
            except httpx.TransportError as error:
                code = transport_error_code(error)
                raise AudioFetchError(f"Audio download failed: {code}") from None
            except httpx.HTTPError:
                raise AudioFetchError("Audio download failed: http_error") from None
        raise AudioSourceError("Too many audio redirects")

    async def get_updates(
        self, *, marker: int | None, timeout_s: int = 30, types: tuple[str, ...] = ()
    ) -> tuple[list[object], int | None]:
        """Long polling (GET /updates) for local runs without a public HTTPS webhook."""
        params: dict[str, str | int] = {"timeout": timeout_s, "limit": 100}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = ",".join(types)
        try:
            response = await self._http.get("/updates", params=params, timeout=timeout_s + 10)
        except httpx.TransportError as error:
            code = transport_error_code(error)
            raise PollingError(f"GET /updates failed: {code}", code=code) from None
        except httpx.HTTPError:
            raise PollingError("GET /updates failed", code="http_error") from None
        if response.status_code != 200:
            code = f"http_{response.status_code}"
            raise PollingError(f"GET /updates failed: {code}", code=code)
        try:
            payload = response.json()
        except ValueError:
            raise PollingError("GET /updates returned invalid JSON", code="invalid_json") from None
        if not isinstance(payload, Mapping) or not isinstance(payload.get("updates", []), list):
            raise PollingError("GET /updates returned an unexpected body", code="unexpected_body")
        next_marker = payload.get("marker")
        return list(payload.get("updates", [])), next_marker if isinstance(next_marker, int) else marker

    async def _subscriptions_call(self, method: str, **kwargs: object) -> object:
        try:
            response = await self._http.request(method, "/subscriptions", **kwargs)  # type: ignore[arg-type]
        except httpx.TransportError as error:
            code = transport_error_code(error)
            raise SubscriptionError(f"{method} /subscriptions failed: {code}", code=code) from None
        if response.status_code != 200:
            code = f"http_{response.status_code}"
            raise SubscriptionError(f"{method} /subscriptions failed: {code}", code=code)
        try:
            return response.json()
        except ValueError:
            raise SubscriptionError("Invalid JSON from /subscriptions", code="invalid_json") from None

    @staticmethod
    def _check_success(payload: object, method: str) -> None:
        if isinstance(payload, dict) and payload.get("success") is False:
            # MAX returns 200 with success=false and a reason; the reason has no secrets of ours.
            reason = payload.get("message")
            text = reason[:200] if isinstance(reason, str) else "unknown reason"
            raise SubscriptionError(f"{method} /subscriptions rejected: {text}", code="rejected")

    async def subscribe(self, *, url: str, secret: str, update_types: tuple[str, ...]) -> None:
        validate_webhook_url(url)
        body = {"url": url, "update_types": list(update_types), "secret": secret}
        self._check_success(await self._subscriptions_call("POST", json=body), "POST")

    async def unsubscribe(self, *, url: str) -> None:
        self._check_success(await self._subscriptions_call("DELETE", params={"url": url}), "DELETE")

    async def check_token(self) -> str:
        """GET /me: "" if the token works, otherwise a short safe error code."""
        try:
            response = await self._http.get("/me")
        except httpx.TransportError as error:
            return transport_error_code(error)
        return "" if response.status_code == 200 else f"http_{response.status_code}"

    async def list_subscriptions(self) -> list[str]:
        """Only the subscribed URLs: other fields are not needed and are not printed."""
        payload = await self._subscriptions_call("GET")
        items = payload.get("subscriptions") if isinstance(payload, dict) else None
        if not isinstance(items, list):
            raise SubscriptionError("Unexpected /subscriptions body", code="unexpected_body")
        return [
            str(item["url"]) for item in items if isinstance(item, dict) and isinstance(item.get("url"), str)
        ]
