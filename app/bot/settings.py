"""Settings of the MAX bot adapter, read from environment variables; nothing connects at import."""

import math
import os
import re
from dataclasses import dataclass, field
from zoneinfo import ZoneInfo

# https://dev.max.ru/docs-api/methods/POST/messages: the docs ask to use this domain.
DEFAULT_API_URL = "https://platform-api2.max.ru"
# POST /messages accepts up to 4000 characters of text.
MAX_TEXT_LENGTH = 4000
# The notification contract limits one external attempt to 20 seconds.
MAX_SEND_TIMEOUT_S = 20.0
# Attempts of one send on 429/5xx/network errors; the lock TTL accounts for all of them.
SEND_ATTEMPTS = 3
# https://dev.max.ru/docs-api/methods/POST/subscriptions
WEBHOOK_SECRET_PATTERN = re.compile(r"[A-Za-z0-9_-]{5,256}")


@dataclass(frozen=True, kw_only=True)
class BotSettings:
    token: str = field(repr=False)
    api_url: str = DEFAULT_API_URL
    # Empty secret is allowed only for long polling; the webhook handler refuses to run without one.
    webhook_secret: str = field(default="", repr=False)
    # Explicit product setting until user profiles store a zone (root AGENTS §7).
    default_timezone: str = "Europe/Moscow"
    # Host suffixes the bot may download audio from; checked for every URL and redirect.
    audio_hosts: tuple[str, ...] = ("max.ru",)
    audio_max_bytes: int = 10 * 1024 * 1024
    send_timeout_s: float = 10.0
    processing_timeout_s: float = 120.0
    busy_ttl_s: int = 180
    max_concurrency: int = 8
    key_prefix: str = "it-gorky"
    # Extra PEM bundle added to the public CAs, e.g. the Russian Trusted Root CA used by MAX.
    ca_bundle_file: str | None = None
    # Optional restriction for a local live smoke. Other chats receive no messages.
    test_chat_id: str | None = None

    def __post_init__(self) -> None:
        if not self.token.strip():
            raise ValueError("MAX_BOT_TOKEN must not be blank")
        if not self.api_url.startswith("https://"):
            raise ValueError("MAX_API_URL must use https")
        if self.webhook_secret and not WEBHOOK_SECRET_PATTERN.fullmatch(self.webhook_secret):
            raise ValueError("MAX_WEBHOOK_SECRET must be 5-256 characters [A-Za-z0-9_-]")
        ZoneInfo(self.default_timezone)  # ZoneInfoNotFoundError for an unknown zone
        hosts = tuple(host.strip().lower().lstrip(".") for host in self.audio_hosts if host.strip())
        if not hosts:
            raise ValueError("MAX_AUDIO_HOSTS must name at least one host")
        object.__setattr__(self, "audio_hosts", hosts)
        if self.audio_max_bytes <= 0:
            raise ValueError("MAX_AUDIO_MAX_BYTES must be positive")
        for name, value in (
            ("MAX_SEND_TIMEOUT_S", self.send_timeout_s),
            ("BOT_PROCESSING_TIMEOUT_S", self.processing_timeout_s),
        ):
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.send_timeout_s > MAX_SEND_TIMEOUT_S:
            raise ValueError("MAX_SEND_TIMEOUT_S must not exceed 20 seconds")
        # The lock must outlive the longest processing, otherwise two requests could overlap.
        if self.busy_ttl_s <= self.processing_timeout_s + SEND_ATTEMPTS * (self.send_timeout_s + 4):
            raise ValueError("BOT_BUSY_TTL_S must exceed processing and send timeouts")
        if self.max_concurrency < 1:
            raise ValueError("BOT_MAX_CONCURRENCY must be positive")
        if not self.key_prefix.strip():
            raise ValueError("Key prefix must not be blank")
        if self.ca_bundle_file is not None and not os.path.isfile(self.ca_bundle_file):
            raise ValueError("MAX_CA_BUNDLE_FILE must point to an existing PEM file")
        if self.test_chat_id is not None and not re.fullmatch(r"-?[0-9]+", self.test_chat_id):
            raise ValueError("MAX_TEST_CHAT_ID must be a numeric chat ID")

    @classmethod
    def from_env(cls) -> "BotSettings":
        hosts = os.environ.get("MAX_AUDIO_HOSTS", "max.ru")
        return cls(
            token=os.environ.get("MAX_BOT_TOKEN", ""),
            api_url=os.environ.get("MAX_API_URL", DEFAULT_API_URL).rstrip("/"),
            webhook_secret=os.environ.get("MAX_WEBHOOK_SECRET", ""),
            default_timezone=os.environ.get("MAX_DEFAULT_TIMEZONE", "Europe/Moscow"),
            audio_hosts=tuple(hosts.split(",")),
            audio_max_bytes=int(os.environ.get("MAX_AUDIO_MAX_BYTES", str(10 * 1024 * 1024))),
            send_timeout_s=float(os.environ.get("MAX_SEND_TIMEOUT_S", "10")),
            processing_timeout_s=float(os.environ.get("BOT_PROCESSING_TIMEOUT_S", "120")),
            busy_ttl_s=int(os.environ.get("BOT_BUSY_TTL_S", "180")),
            max_concurrency=int(os.environ.get("BOT_MAX_CONCURRENCY", "8")),
            ca_bundle_file=os.environ.get("MAX_CA_BUNDLE_FILE") or None,
            test_chat_id=os.environ.get("MAX_TEST_CHAT_ID") or None,
        )
