from datetime import datetime, timedelta
import json
from pathlib import Path
import tempfile
import unittest
import zoneinfo

from reducer.race import (
    FIELDS,
    MAX_BYTES,
    RaceValidationError,
    check_published_feeds,
    encode,
    main,
    validate_summary,
)


ZONE = zoneinfo.ZoneInfo("America/Toronto")
NOW = datetime(2026, 9, 27, 19, 7, tzinfo=ZONE)


def summary(**changes):
    value = {
        "version": 1,
        "week_start": "2026-09-21",
        "week_end": "2026-09-27",
        "timezone": "America/Toronto",
        "generated_at": "2026-09-27T19:05:00-04:00",
        "a": 5200,
        "b": 4600,
    }
    value.update(changes)
    return value


class SummaryValidationTests(unittest.TestCase):
    def test_valid_summary_round_trips_to_compact_json(self):
        payload = encode(validate_summary(summary(), NOW))
        self.assertEqual(json.loads(payload), summary())
        self.assertEqual(list(json.loads(payload)), list(FIELDS))
        self.assertLess(len(payload), MAX_BYTES)
        self.assertNotIn(b" ", payload)

    def test_zero_totals_are_valid(self):
        self.assertEqual(validate_summary(summary(a=0, b=0), NOW)["a"], 0)

    def test_sunday_evening_and_monday_reset(self):
        late = summary(generated_at="2026-09-27T23:59:00-04:00")
        validate_summary(late, datetime(2026, 9, 27, 23, 59, 30, tzinfo=ZONE))
        monday = summary(week_start="2026-09-28", week_end="2026-10-04",
                         generated_at="2026-09-28T00:00:05-04:00")
        validate_summary(monday, datetime(2026, 9, 28, 0, 1, tzinfo=ZONE))
        with self.assertRaises(RaceValidationError):
            # Last week's summary generated after Monday 00:00 is rejected.
            validate_summary(summary(generated_at="2026-09-28T00:00:05-04:00"),
                             datetime(2026, 9, 28, 0, 1, tzinfo=ZONE))

    def test_standard_time_offset_is_accepted(self):
        winter = summary(week_start="2026-12-14", week_end="2026-12-20",
                         generated_at="2026-12-20T18:00:00-05:00")
        validate_summary(winter, datetime(2026, 12, 20, 18, 1, tzinfo=ZONE))

    def test_malformed_summaries_are_rejected(self):
        bad = [
            summary(version=2), summary(version=True), summary(timezone="UTC"),
            summary(week_start="2026-09-22", week_end="2026-09-28"),
            summary(week_end="2026-09-28"), summary(week_start="2026-02-30"),
            summary(week_start="21/09/2026"),
            summary(generated_at="2026-09-28T19:05:00-04:00"),
            summary(generated_at="2026-09-27T19:05:00"),
            summary(generated_at="2026-09-27T23:05:00+00:00"),
            summary(generated_at="2026-09-27T19:05:00-05:00"),
            summary(generated_at=12345), summary(a=-1), summary(b=1.5),
            summary(a=True), summary(b="4600"), summary(a=1_000_001),
            [1, 2], "summary", {"ok": False, "error_code": "unavailable"},
        ]
        missing = summary()
        del missing["b"]
        bad.append(missing)
        for data in bad:
            with self.subTest(data=data), self.assertRaises(RaceValidationError):
                validate_summary(data, NOW)

    def test_unexpected_fields_fail_closed_rather_than_publish(self):
        for extra in ({"sessions": []}, {"names": ["x", "y"]}, {"a_label": "x"}):
            with self.subTest(extra=extra), self.assertRaises(RaceValidationError):
                validate_summary(summary(**extra), NOW)

    def test_stale_or_future_summary_is_rejected(self):
        with self.assertRaises(RaceValidationError):
            validate_summary(summary(), NOW + timedelta(minutes=11))
        with self.assertRaises(RaceValidationError):
            validate_summary(summary(), NOW - timedelta(minutes=10))
        validate_summary(summary(), NOW + timedelta(minutes=7))  # 9 minutes old

    def test_carry_forward_window_accepts_an_older_published_file(self):
        later = NOW + timedelta(days=2)
        with self.assertRaises(RaceValidationError):
            validate_summary(summary(), later)
        self.assertEqual(
            validate_summary(summary(), later, timedelta(days=8))["a"], 5200
        )


class PublishedFeedTests(unittest.TestCase):
    def pair(self):
        schedule = {"version": 2, "contentHash": "f" * 64,
                    "validity": {"sourceStatus": "valid"}}
        notices = {"version": 1, "scheduleContentHash": "f" * 64}
        return schedule, notices

    def test_matching_valid_pair_passes(self):
        check_published_feeds(*self.pair())

    def test_bad_pairs_are_rejected(self):
        cases = []
        for key, value in (("version", 1), ("contentHash", "short"),
                           ("validity", {"sourceStatus": "stale"})):
            schedule, notices = self.pair()
            schedule[key] = value
            cases.append((schedule, notices))
        schedule, notices = self.pair()
        notices["scheduleContentHash"] = "e" * 64
        cases.append((schedule, notices))
        cases.append((None, {}))
        for schedule, notices in cases:
            with self.subTest(schedule=schedule), self.assertRaises(RaceValidationError):
                check_published_feeds(schedule, notices)


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.base = Path(self._dir.name)

    def tearDown(self):
        self._dir.cleanup()

    def run_summary(self, raw: bytes, command="summary", now=NOW):
        source = self.base / "raw.json"
        output = self.base / "site" / "race.json"
        source.write_bytes(raw)
        code = main([command, "--input", str(source), "--output", str(output),
                     "--now", now.isoformat()])
        return code, output

    def test_valid_summary_is_written_atomically(self):
        code, output = self.run_summary(json.dumps(summary()).encode())
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.read_bytes()), summary())
        self.assertEqual([p.name for p in output.parent.iterdir()], ["race.json"])

    def test_rejected_summary_writes_nothing_and_echoes_no_values(self):
        for raw in (b"", b"<html>sign in</html>", b"{" + b" " * 5000 + b"}",
                    json.dumps(summary(a=-7)).encode()):
            with self.subTest(raw=raw[:20]):
                code, output = self.run_summary(raw)
                self.assertEqual(code, 1)
                self.assertFalse(output.exists())

    def test_carry_accepts_a_published_file_from_earlier_in_the_week(self):
        code, output = self.run_summary(
            json.dumps(summary()).encode(), "carry", NOW + timedelta(days=1)
        )
        self.assertEqual(code, 0)
        self.assertTrue(output.exists())


if __name__ == "__main__":
    unittest.main()
