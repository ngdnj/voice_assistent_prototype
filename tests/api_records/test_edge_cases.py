"""REST and initData edge cases found in review; each defect test guards its fix."""

import unittest
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from fastapi import FastAPI

from app.api.init_data import InvalidInitDataError, verify_init_data
from app.api.records import INIT_DATA_HEADER, build_records_router
from app.mcp_server.tools import EventTools
from tests.api_records import BOT_TOKEN, signed_init_data

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)


def verify(init_data: str) -> str:
    return verify_init_data(init_data, bot_token=BOT_TOKEN, now=NOW, max_age=timedelta(days=1)).owner_id


class InitDataEdgeTests(unittest.TestCase):
    def test_repeated_parameter_is_rejected(self) -> None:
        with self.assertRaises(InvalidInitDataError):
            verify(signed_init_data(auth_date=NOW) + "&query_id=q-1")

    def test_future_skew_is_one_minute(self) -> None:
        self.assertEqual(verify(signed_init_data(auth_date=NOW + timedelta(seconds=59))), "67890")
        with self.assertRaises(InvalidInitDataError):
            verify(signed_init_data(auth_date=NOW + timedelta(minutes=2)))

    def test_user_id_must_be_a_positive_json_integer(self) -> None:
        for user_id in ("123", 123.0, True, 0, -5, None):
            with self.subTest(user_id=user_id), self.assertRaises(InvalidInitDataError):
                verify(signed_init_data(user_id=user_id, auth_date=NOW))

    def test_url_encoded_values_are_decoded_before_signing(self) -> None:
        # Values with "=", "&" and "+" are percent-encoded by the signer and decoded once by the server.
        init_data = signed_init_data(auth_date=NOW, extra={"start_param": "a=b&c+d %"})
        self.assertEqual(verify(init_data), "67890")
        tampered = init_data.replace("start_param=", "start_param=x")
        with self.assertRaises(InvalidInitDataError):
            verify(tampered)


class RestOrderingEdgeTests(unittest.IsolatedAsyncioTestCase):
    """No storage: these requests must be answered before EventTools is reached."""

    async def asyncSetUp(self) -> None:
        app = FastAPI()
        tools = EventTools(None, None, None, None)  # type: ignore[arg-type]
        app.include_router(build_records_router(tools, bot_token=BOT_TOKEN, clock=lambda: NOW))
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        self.client = httpx.AsyncClient(transport=transport, base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_unauthenticated_bad_limit_is_401(self) -> None:
        # INTEGRATION §4: without valid initData every route answers 401 before parameter checks.
        # Fact: "²".isdigit() is True and int("²") raises in records.py, the answer is a bare 500.
        response = await self.client.get("/api/events/search", params={"query": "x", "limit": "²"})
        self.assertEqual(response.status_code, 401)

    async def test_unauthenticated_malformed_body_is_401(self) -> None:
        # Fact: FastAPI validates the dict body first and answers 422 in its own {"detail": ...} format.
        for content in (b"[1]", b"{bad"):
            with self.subTest(content=content):
                response = await self.client.patch(
                    f"/api/tasks/{uuid4()}/status",
                    content=content,
                    headers={"content-type": "application/json"},
                )
                self.assertEqual(response.status_code, 401)

    async def test_authenticated_unicode_digit_limit_is_422(self) -> None:
        headers = {INIT_DATA_HEADER: signed_init_data(auth_date=NOW)}
        response = await self.client.get(
            "/api/events/search", params={"query": "x", "limit": "²", "timezone": "UTC"}, headers=headers
        )
        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["field"], "limit")
