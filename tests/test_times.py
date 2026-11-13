"""Times and sizes as agentbox writes and reads them (agentbox/times.py).

Local times are checked in a fixed time zone (TZ, Europe/Berlin), restored afterwards.

Run from the repo root: python3 -m unittest discover -s tests
"""
import datetime
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from agentbox import times  # noqa: E402

UTC = datetime.timezone.utc
EPOCH = 1794562200                   # 2026-11-13T09:30:00Z, 10:30 in Berlin


class LocalTimeZone(unittest.TestCase):
    def setUp(self):
        old = os.environ.get("TZ")

        def restore():
            if old is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = old
            time.tzset()
        self.addCleanup(restore)
        os.environ["TZ"] = "Europe/Berlin"
        time.tzset()


class UtcTest(unittest.TestCase):
    def test_iso_to_the_second(self):
        self.assertEqual(times.iso(EPOCH), "2026-11-13T09:30:00Z")
        self.assertEqual(times.iso(EPOCH + 0.9), "2026-11-13T09:30:00Z")
        self.assertEqual(times.iso(0), "1970-01-01T00:00:00Z")

    def test_iso_of_no_number_is_none(self):
        for v in (None, "1794562200", True, [EPOCH]):
            self.assertIsNone(times.iso(v), v)

    def test_parse_and_epoch_take_a_final_z(self):
        self.assertEqual(times.parse("2026-11-13T09:30:00Z"), datetime.datetime(2026, 11, 13, 9, 30, tzinfo=UTC))
        self.assertEqual(times.epoch("2026-11-13T09:30:00Z"), EPOCH)
        self.assertEqual(times.epoch("2026-11-13T10:30:00+01:00"), EPOCH)
        self.assertEqual(times.epoch(times.iso(EPOCH)), EPOCH)

    def test_parse_and_epoch_of_no_iso_time_are_none(self):
        for v in (None, 5, "", "yesterday", "2026-13-01T00:00:00Z"):
            self.assertIsNone(times.parse(v), v)
            self.assertIsNone(times.epoch(v), v)

    def test_journal_time_to_the_microsecond(self):
        e = {"__REALTIME_TIMESTAMP": str(EPOCH * 10**6 + 123456)}
        self.assertEqual(times.journal_usec(e), EPOCH * 10**6 + 123456)
        self.assertEqual(times.journal_time(e), (EPOCH * 10**6 + 123456, "2026-11-13T09:30:00.123456Z"))
        self.assertEqual(times.journal_time({"__REALTIME_TIMESTAMP": "0"}), (0, "1970-01-01T00:00:00.000000Z"))

    def test_a_journal_record_without_a_usable_time(self):
        for e in ({}, {"__REALTIME_TIMESTAMP": None}, {"__REALTIME_TIMESTAMP": "x"}, None):
            self.assertIsNone(times.journal_usec(e), e)
            self.assertIsNone(times.journal_time(e), e)
        # a number, but out of range
        self.assertIsNone(times.journal_time({"__REALTIME_TIMESTAMP": "9" * 30}))


class HostLocalTest(LocalTimeZone):
    def test_local_to_the_minute(self):
        self.assertEqual(times.local(EPOCH), "2026-11-13 10:30")
        self.assertEqual(times.local(EPOCH + 59.5), "2026-11-13 10:30")

    def test_local_of_no_number(self):
        for v in (None, "1794562200"):
            self.assertEqual(times.local(v), "?")

    def test_date_time_with_an_optional_time(self):
        self.assertEqual(times.date_time("2026-11-13"), datetime.datetime(2026, 11, 13))
        self.assertEqual(times.date_time("2026-11-13 10:30"), datetime.datetime(2026, 11, 13, 10, 30))
        self.assertEqual(times.date_time("2026-11-13T10:30:15"), datetime.datetime(2026, 11, 13, 10, 30, 15))
        self.assertEqual(times.date_time("2026-11-13 10:30").timestamp(), EPOCH)

    def test_date_time_not_written_that_way_or_no_real_day(self):
        for v in ("today", "-2h", "2026-11-13 10", "2026-11-13 10:30:00Z"):
            self.assertIsNone(times.date_time(v), v)
        with self.assertRaises(ValueError):
            times.date_time("2026-02-30")

    def test_time_option_values(self):
        for v in ("today", "yesterday", "now", "-15min", "-2w", "2026-11-13", "2026-11-13 10:30",
                  "2026-11-13T10:30:15"):
            self.assertTrue(times.TIME.fullmatch(v), v)
        for v in ("tomorrow", "-2y", "-1234567s", "13.11.2026", "2026-11-13 10"):
            self.assertFalse(times.TIME.fullmatch(v), v)


class SizeTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(times.size(0), "0 bytes")
        self.assertEqual(times.size(1023), "1023 bytes")
        self.assertEqual(times.size(1536), "1.5 KiB")
        self.assertEqual(times.size(5 * 2**20), "5.0 MiB")
        self.assertEqual(times.size(3 * 2**40), "3.0 TiB")
        self.assertEqual(times.size(2**60), f"{2**20:.1f} TiB")

    def test_largest_unit(self):
        self.assertEqual(times.size(3 * 2**40, "GiB"), "3072.0 GiB")
        self.assertEqual(times.size(1536, "GiB"), "1.5 KiB")


if __name__ == "__main__":
    unittest.main()
