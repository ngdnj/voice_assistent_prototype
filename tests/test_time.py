import unittest
from datetime import UTC, date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.planner.task import create_task
from app.planner.time import (
    AmbiguousLocalTimeError,
    NonexistentLocalTimeError,
    day_bounds_utc,
    is_in_period,
    local_to_utc,
    to_utc,
)


class TimeTests(unittest.TestCase):
    def test_different_zones_and_midnight_crossing(self) -> None:
        for name, hour in (("Europe/Moscow", 21), ("Asia/Tokyo", 15)):
            with self.subTest(zone=name):
                instant = local_to_utc(datetime(2026, 10, 22, 0, 30), name)
                self.assertEqual(instant, datetime(2026, 10, 21, hour, 30, tzinfo=UTC))
                self.assertIs(instant.tzinfo, UTC)
        self.assertEqual(
            local_to_utc(datetime(2026, 1, 15, 23, 30), "America/New_York"),
            datetime(2026, 1, 16, 4, 30, tzinfo=UTC),
        )

    def test_aware_conversion(self) -> None:
        expected = datetime(2026, 10, 21, 21, 30, tzinfo=UTC)
        self.assertEqual(to_utc(expected), expected)
        self.assertEqual(
            to_utc(datetime(2026, 10, 22, 0, 30, tzinfo=ZoneInfo("Europe/Moscow"))),
            expected,
        )

    def test_naive_datetime_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            to_utc(datetime(2026, 10, 22))
        with self.assertRaises(ValueError):
            local_to_utc(datetime(2026, 10, 22, tzinfo=UTC), "Europe/Moscow")

    def test_date_is_separate_from_datetime(self) -> None:
        day = date(2026, 10, 22)
        with self.assertRaises(TypeError):
            to_utc(day)
        with self.assertRaises(TypeError):
            local_to_utc(day, "Europe/Moscow")
        with self.assertRaises(TypeError):
            day_bounds_utc(datetime(2026, 10, 22), "Europe/Moscow")
        self.assertEqual(day, date(2026, 10, 22))

    def test_invalid_zone(self) -> None:
        with self.assertRaises(ZoneInfoNotFoundError):
            day_bounds_utc(date(2026, 10, 22), "Invalid/Zone")
        with self.assertRaises(ZoneInfoNotFoundError):
            local_to_utc(datetime(2026, 10, 22), "Invalid/Zone")

    def test_day_bounds_and_half_open_interval(self) -> None:
        start, end = day_bounds_utc(date(2026, 10, 22), "Europe/Moscow")
        self.assertEqual(start, datetime(2026, 10, 21, 21, tzinfo=UTC))
        self.assertEqual(end, datetime(2026, 10, 22, 21, tzinfo=UTC))
        self.assertFalse(is_in_period(start - timedelta(microseconds=1), start, end))
        self.assertTrue(is_in_period(start, start, end))
        self.assertTrue(is_in_period(end - timedelta(microseconds=1), start, end))
        self.assertFalse(is_in_period(end, start, end))
        for boundary in (start, start - timedelta(seconds=1)):
            with self.assertRaises(ValueError):
                is_in_period(start, start, boundary)

    def test_day_length_across_dst(self) -> None:
        for day, hours in ((date(2026, 3, 29), 23), (date(2026, 10, 25), 25)):
            with self.subTest(day=day):
                start, end = day_bounds_utc(day, "Europe/Berlin")
                self.assertEqual(end - start, timedelta(hours=hours))
                self.assertEqual(start.astimezone(ZoneInfo("Europe/Berlin")).date(), day)
                self.assertEqual(
                    end.astimezone(ZoneInfo("Europe/Berlin")).date(), day + timedelta(days=1)
                )

    def test_interval_compares_instants_and_rejects_naive_bounds(self) -> None:
        start, end = day_bounds_utc(date(2026, 10, 22), "Europe/Moscow")
        local = datetime(2026, 10, 22, 0, tzinfo=ZoneInfo("Europe/Moscow"))
        self.assertTrue(is_in_period(local, start, end))
        for values in (
            (start.replace(tzinfo=None), start, end),
            (start, start.replace(tzinfo=None), end),
            (start, start, end.replace(tzinfo=None)),
        ):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    is_in_period(*values)

    def test_gap_and_fold_rejected(self) -> None:
        for wall_time, error in (
            (datetime(2026, 3, 29, 2, 30), NonexistentLocalTimeError),
            (datetime(2026, 10, 25, 2, 30), AmbiguousLocalTimeError),
        ):
            with self.subTest(wall_time=wall_time):
                with self.assertRaises(error):
                    local_to_utc(wall_time, "Europe/Berlin")
                for fold in (0, 1):
                    aware = wall_time.replace(tzinfo=ZoneInfo("Europe/Berlin"), fold=fold)
                    with self.assertRaises(error):
                        to_utc(aware)
                    with self.assertRaises(error):
                        create_task(owner_id="user-42", title="Отчёт", created_at=aware)

    def test_explicit_offset_disambiguates_instant(self) -> None:
        for offset, utc_hour in ((2, 0), (1, 1)):
            with self.subTest(offset=offset):
                instant = datetime(2026, 10, 25, 2, 30, tzinfo=timezone(timedelta(hours=offset)))
                self.assertEqual(to_utc(instant), datetime(2026, 10, 25, utc_hour, 30, tzinfo=UTC))

    def test_invalid_midnight_is_not_shifted(self) -> None:
        for day, zone, error in (
            (date(2011, 12, 30), "Pacific/Apia", NonexistentLocalTimeError),
            (date(2011, 12, 29), "Pacific/Apia", NonexistentLocalTimeError),
            (date(2026, 3, 8), "America/Havana", NonexistentLocalTimeError),
            (date(2026, 11, 1), "America/Havana", AmbiguousLocalTimeError),
        ):
            with self.subTest(day=day, zone=zone):
                with self.assertRaises(error):
                    day_bounds_utc(day, zone)
