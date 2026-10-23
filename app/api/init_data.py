"""Server-side validation of MAX Mini App initData (https://dev.max.ru/docs/webapps/validation).

The browser sends the raw `window.WebApp.initData` string; initDataUnsafe and any user_id from the
client are never trusted. The signature algorithm is MAX's own and is not copied from another messenger.
"""

import hashlib
import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote

MAX_INIT_DATA_LENGTH = 8192
# Accepted clock difference between the MAX server and ours for auth_date in the future.
FUTURE_SKEW = timedelta(minutes=1)


class InvalidInitDataError(Exception):
    """initData is missing, malformed, wrongly signed or too old; the reason is not shown to clients."""


@dataclass(frozen=True)
class MaxUser:
    # Same identity as the bot's normalized message: str(MAX user id).
    owner_id: str
    auth_date: datetime


def _pairs(init_data: str) -> dict[str, str]:
    pairs: dict[str, str] = {}
    for part in init_data.split("&"):
        key, separator, value = part.partition("=")
        if not separator or not key or key in pairs:
            # Every parameter, including hash, must appear exactly once.
            raise InvalidInitDataError("Malformed initData")
        pairs[key] = unquote(value)
    return pairs


def verify_init_data(init_data: str, *, bot_token: str, now: datetime, max_age: timedelta) -> MaxUser:
    """Check the HMAC-SHA256 signature and freshness, then return the trusted MAX user."""
    if not init_data or len(init_data) > MAX_INIT_DATA_LENGTH:
        raise InvalidInitDataError("initData is missing or too long")
    pairs = _pairs(init_data)
    received = pairs.pop("hash", None)
    if received is None:
        raise InvalidInitDataError("initData has no hash")
    check_string = "\n".join(f"{key}={pairs[key]}" for key in sorted(pairs))
    secret = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received.lower()):
        raise InvalidInitDataError("initData signature mismatch")
    try:
        auth_date = datetime.fromtimestamp(int(pairs["auth_date"]), UTC)
        user = json.loads(pairs["user"])
        user_id = user["id"]
    except (KeyError, ValueError, TypeError, OverflowError):
        raise InvalidInitDataError("initData has no valid auth_date or user") from None
    if not now - max_age <= auth_date <= now + FUTURE_SKEW:
        raise InvalidInitDataError("initData is expired")
    if type(user_id) is not int or user_id <= 0:
        raise InvalidInitDataError("initData user id is invalid")
    return MaxUser(owner_id=str(user_id), auth_date=auth_date)
