"""Trusted caller context: set by the authenticated backend, never by the model's tool arguments."""

import hmac
from collections.abc import Mapping
from dataclasses import dataclass
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

MAX_HEADER_LENGTH = 200


class UnauthorizedError(Exception):
    """The call lacks a valid service token or trusted user context."""


@dataclass(frozen=True)
class TrustedContext:
    owner_id: str
    timezone: str
    request_id: str
    # Identity of one logical creation; required by create_event only.
    action_id: str | None


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name.lower())
    if value is None or not value.strip():
        return None
    if len(value) > MAX_HEADER_LENGTH:
        raise UnauthorizedError(f"{name} is too long")
    return value.strip()


def context_from_headers(headers: Mapping[str, str], *, service_token: str) -> TrustedContext:
    """Validate lower-cased HTTP headers of an internal call from the agent worker."""
    authorization = headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    # Constant-time comparison; the token itself is never logged or echoed.
    if scheme.lower() != "bearer" or not hmac.compare_digest(token.encode(), service_token.encode()):
        raise UnauthorizedError("Invalid service token")
    owner_id = _header(headers, "X-Owner-Id")
    timezone = _header(headers, "X-Timezone")
    if owner_id is None or timezone is None:
        raise UnauthorizedError("X-Owner-Id and X-Timezone are required")
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise UnauthorizedError("X-Timezone must be an IANA time zone") from None
    return TrustedContext(
        owner_id=owner_id,
        timezone=timezone,
        request_id=_header(headers, "X-Request-Id") or str(uuid4()),
        action_id=_header(headers, "X-Action-Id"),
    )
