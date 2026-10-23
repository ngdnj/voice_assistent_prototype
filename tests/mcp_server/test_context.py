import unittest

from app.mcp_server.context import UnauthorizedError, context_from_headers

TOKEN = "t" * 32
GOOD = {
    "authorization": f"Bearer {TOKEN}",
    "x-owner-id": "user-42",
    "x-timezone": "Europe/Moscow",
    "x-request-id": "req-1",
    "x-action-id": "job-1:0",
}


class TrustedContextTests(unittest.TestCase):
    def test_valid_headers(self) -> None:
        context = context_from_headers(GOOD, service_token=TOKEN)
        self.assertEqual(
            (context.owner_id, context.timezone, context.request_id, context.action_id),
            ("user-42", "Europe/Moscow", "req-1", "job-1:0"),
        )

    def test_optional_ids(self) -> None:
        headers = {key: value for key, value in GOOD.items() if key not in ("x-request-id", "x-action-id")}
        context = context_from_headers(headers, service_token=TOKEN)
        self.assertIsNone(context.action_id)
        self.assertTrue(context.request_id)

    def test_rejected_headers(self) -> None:
        cases = {
            "no token": {k: v for k, v in GOOD.items() if k != "authorization"},
            "wrong token": {**GOOD, "authorization": "Bearer " + "x" * 32},
            "token prefix": {**GOOD, "authorization": f"Bearer {TOKEN[:-1]}"},
            "basic scheme": {**GOOD, "authorization": f"Basic {TOKEN}"},
            "no owner": {k: v for k, v in GOOD.items() if k != "x-owner-id"},
            "blank owner": {**GOOD, "x-owner-id": "  "},
            "long owner": {**GOOD, "x-owner-id": "u" * 201},
            "no timezone": {k: v for k, v in GOOD.items() if k != "x-timezone"},
            "unknown timezone": {**GOOD, "x-timezone": "Mars/Olympus"},
        }
        for case, headers in cases.items():
            with self.subTest(case=case), self.assertRaises(UnauthorizedError) as caught:
                context_from_headers(headers, service_token=TOKEN)
            self.assertNotIn(TOKEN, str(caught.exception))
