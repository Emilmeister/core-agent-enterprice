"""Pinned parser dialect and real timezone transition contracts."""
import unittest
from datetime import UTC, datetime, timedelta, timezone

from core_agent.cron_expression import next_due, normalize_expression
from core_agent.errors import CoreError


def instant(value):
    return datetime.fromisoformat(value).replace(tzinfo=UTC)


class CronExpressionTests(unittest.TestCase):
    def assert_invalid(self, callback):
        with self.assertRaises(CoreError) as error:
            callback()
        self.assertEqual(error.exception.code, "CRON_INVALID")
        self.assertEqual(str(error.exception), "CRON_INVALID")

    def test_normalizes_whitespace_and_accepts_numeric_and_named_dialect(self):
        self.assertEqual(normalize_expression("  */15\t9-17  * JAN,MAR MON-FRI \n"), "*/15 9-17 * jan,mar mon-fri")
        for value in ("* * * * *", "0,15,30,45 8-18/2 * * 0,7", "0 0 1-31 jan-dec sun-sat",
                      "5-5/2 0 1/4 * *", "0 0 * * 7/2", "0 0 * * 0-7/2"):
            with self.subTest(value=value):
                self.assertIsInstance(normalize_expression(value), str)

    def test_rejects_unsupported_extensions_ranges_and_unsafe_inputs(self):
        invalid = [None, True, 12, b"* * * * *", "", "* * * *", "* * * * * *", "0 0 1 1 * 2027",
                   "@daily", "H * * * *", "R * * * *", "0 0 L * *", "0 0 1W * *", "0 0 * * MON#2",
                   "0 0 ? * *", "0 0 * * 5-1", "0 0 * DEC-JAN *", "59-0 * * * *", "* * * * *\0",
                   "* * * * *\ud800", "* * * * *" + " " * 256, "*/0 * * * *", "*/-1 * * * *",
                   "1,,2 * * * *", "1-2-3 * * * *", "0 0 0 * *", "0 0 * 0 *", "60 * * * *",
                   "0 24 * * *", "0 0 32 * *", "0 0 * 13 *", "0 0 * * 8", "0 0 * january *",
                   "0 0 mon * *", "0 0 * * monday", "1.5 * * * *", "+1 * * * *", "０ * * * *"]
        for value in invalid:
            with self.subTest(value=repr(value)):
                self.assert_invalid(lambda: normalize_expression(value))

    def test_next_is_strict_and_default_timezone_is_moscow(self):
        self.assertEqual(next_due("0 9 * * *", after_utc=instant("2026-09-30T05:59:59")), instant("2026-09-30T06:00:00"))
        self.assertEqual(next_due("0 9 * * *", after_utc=instant("2026-09-30T06:00:00")), instant("2026-10-01T06:00:00"))
        result = next_due("* * * * *", "UTC", after_utc=datetime(2026, 1, 1, 3, 0, 0, 1, timezone(timedelta(hours=3))))
        self.assertEqual(result, instant("2026-01-01T00:01:00"))
        self.assertIs(result.tzinfo, UTC)

    def test_or_semantics_survives_impossible_dom_and_explicit_full_range(self):
        cases = [("0 0 31 2 mon", "2026-02-02T00:00:00"),
                 ("0 0 30 2 mon-fri", "2026-02-02T00:00:00"),
                 ("0 0 31 2 0-7", "2026-02-02T00:00:00"),
                 ("0 0 31 2 sun", "2026-02-08T00:00:00"),
                 ("0 0 31 1-2 mon", "2026-02-02T00:00:00")]
        for expression, expected in cases:
            with self.subTest(expression=expression):
                self.assertEqual(next_due(expression, "UTC", after_utc=instant("2026-02-01T00:00:00")), instant(expected))
        self.assertEqual(next_due("0 0 1 * mon", "UTC", after_utc=instant("2026-01-30T00:00:00")), instant("2026-02-01T00:00:00"))
        self.assertEqual(next_due("0 0 1 * mon", "UTC", after_utc=instant("2026-02-01T00:00:00")), instant("2026-02-02T00:00:00"))
        self.assertEqual(next_due("0 0 31 2 0-7", "UTC", after_utc=instant("9999-02-01T00:00:00")), instant("9999-02-02T00:00:00"))

    def test_equal_ranges_and_sunday_alias_do_not_expand_unexpectedly(self):
        for expression, expected in (("5-5/2 * * * *", "2026-02-02T00:05:00"),
                                     ("0 0 * * 7-7", "2026-02-08T00:00:00"),
                                     ("0 0 * * 7/2", "2026-02-08T00:00:00"),
                                     ("0 0 * * 0", "2026-02-08T00:00:00"),
                                     ("0 0 * * 5-7/2", "2026-02-06T00:00:00")):
            with self.subTest(expression=expression):
                self.assertEqual(next_due(expression, "UTC", after_utc=instant("2026-02-02T00:00:00")), instant(expected))

    def test_impossible_calendar_and_invalid_timezone_or_after_are_safe(self):
        for expression in ("0 0 31 2 *", "0 0 31 4 *", "0 0 30 2 *"):
            self.assert_invalid(lambda: next_due(expression, "UTC", after_utc=instant("2026-01-01T00:00:00")))
        for zone in (None, True, "", "invalid/private-secret", "../UTC", "UTC\0", "\ud800"):
            self.assert_invalid(lambda: next_due("* * * * *", zone, after_utc=instant("2026-01-01T00:00:00")))
        for after in (None, True, "2026-01-01", datetime(2026, 1, 1), datetime.max.replace(tzinfo=UTC)):
            self.assert_invalid(lambda: next_due("* * * * *", "UTC", after_utc=after))
        self.assertEqual(next_due("0 0 29 2 *", "UTC", after_utc=instant("2096-03-01T00:00:00")), instant("2104-02-29T00:00:00"))

    def test_berlin_gap_is_skipped_and_fold_first_occurrence_runs_once(self):
        self.assertEqual(next_due("30 2 * * *", "Europe/Berlin", after_utc=instant("2026-03-28T02:00:00")), instant("2026-03-30T00:30:00"))
        self.assertEqual(next_due("30 2 * * *", "Europe/Berlin", after_utc=instant("2026-10-24T23:00:00")), instant("2026-10-25T00:30:00"))
        for after in ("2026-10-25T00:30:00", "2026-10-25T00:45:00", "2026-10-25T01:00:00"):
            self.assertEqual(next_due("30 2 * * *", "Europe/Berlin", after_utc=instant(after)), instant("2026-10-26T01:30:00"))
        self.assertEqual(next_due("* * * * *", "Europe/Berlin", after_utc=instant("2026-10-25T00:59:00")), instant("2026-10-25T02:00:00"))

    def test_lord_howe_half_hour_gap_and_fold(self):
        self.assertEqual(next_due("15 2 * * *", "Australia/Lord_Howe", after_utc=instant("2026-10-03T12:00:00")), instant("2026-10-04T15:15:00"))
        self.assertEqual(next_due("45 1 * * *", "Australia/Lord_Howe", after_utc=instant("2026-04-04T13:00:00")), instant("2026-04-04T14:45:00"))
        for after in ("2026-04-04T14:45:00", "2026-04-04T14:50:00", "2026-04-04T15:00:00"):
            self.assertEqual(next_due("45 1 * * *", "Australia/Lord_Howe", after_utc=instant(after)), instant("2026-04-05T15:15:00"))
