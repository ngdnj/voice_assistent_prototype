"""Role 4 Mini App REST tests and an initData signer that follows the MAX documentation step by step."""

import hashlib
import hmac
import json
from datetime import datetime
from typing import Any
from urllib.parse import quote

BOT_TOKEN = "test-bot-token-not-real"


def signed_init_data(
    *, user_id: Any = 67890, auth_date: datetime, token: str = BOT_TOKEN, extra: dict[str, str] | None = None
) -> str:
    """Build initData like MAX does: sorted key=value lines joined by \\n, HMAC key from "WebAppData"."""
    user = {"id": user_id, "first_name": "Иван", "last_name": "", "username": None, "language_code": "ru"}
    params = {
        "auth_date": str(int(auth_date.timestamp())),
        "query_id": "q-1",
        "user": json.dumps(user, ensure_ascii=False),
        **(extra or {}),
    }
    check_string = "\n".join(f"{key}={params[key]}" for key in sorted(params))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    params["hash"] = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    return "&".join(f"{key}={quote(value, safe='')}" for key, value in params.items())
