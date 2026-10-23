import unittest
from datetime import UTC, datetime, timedelta

from app.api.init_data import InvalidInitDataError, verify_init_data
from tests.api_records import BOT_TOKEN, signed_init_data

NOW = datetime(2026, 10, 23, 12, tzinfo=UTC)
DAY = timedelta(days=1)


def verify(init_data: str):  # type: ignore[no-untyped-def]
    return verify_init_data(init_data, bot_token=BOT_TOKEN, now=NOW, max_age=DAY)


class InitDataTests(unittest.TestCase):
    def test_valid_signature_gives_trusted_owner(self) -> None:
        user = verify(signed_init_data(auth_date=NOW - timedelta(hours=1)))
        self.assertEqual(user.owner_id, "67890")
        self.assertEqual(user.auth_date, NOW - timedelta(hours=1))

    def test_freshness_window_is_one_day(self) -> None:
        self.assertEqual(verify(signed_init_data(auth_date=NOW - DAY)).owner_id, "67890")
        for auth_date in (NOW - DAY - timedelta(seconds=1), NOW + timedelta(minutes=2)):
            with self.subTest(auth_date=auth_date), self.assertRaises(InvalidInitDataError):
                verify(signed_init_data(auth_date=auth_date))

    def test_forged_or_malformed_data_is_rejected(self) -> None:
        good = signed_init_data(auth_date=NOW)
        cases = {
            "empty": "",
            "wrong bot token": signed_init_data(auth_date=NOW, token="other-bot-token"),
            "changed user": good.replace("67890", "67891"),
            "added parameter": good + "&chat=1",
            "no hash": "&".join(part for part in good.split("&") if not part.startswith("hash=")),
            "duplicate key": good + "&query_id=q-2",
            "duplicate hash": good + "&hash=" + "0" * 64,
            "no separator": good + "&broken",
            "too long": good + "&x=" + "a" * 9000,
            "string user id": signed_init_data(auth_date=NOW, user_id="67890"),
            "boolean user id": signed_init_data(auth_date=NOW, user_id=True),
            "negative user id": signed_init_data(auth_date=NOW, user_id=-1),
        }
        for case, init_data in cases.items():
            with self.subTest(case=case), self.assertRaises(InvalidInitDataError):
                verify(init_data)

    def test_signed_data_without_user_or_date_is_rejected(self) -> None:
        # Correctly signed but incomplete data does not identify a user.
        import hashlib
        import hmac

        check_string = "query_id=q-1"
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        digest = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
        with self.assertRaises(InvalidInitDataError):
            verify(f"query_id=q-1&hash={digest}")

    def test_error_text_does_not_reveal_the_token(self) -> None:
        with self.assertRaises(InvalidInitDataError) as caught:
            verify(signed_init_data(auth_date=NOW, token="other"))
        self.assertNotIn(BOT_TOKEN, str(caught.exception))
