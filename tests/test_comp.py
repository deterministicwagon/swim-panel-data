"""Anonymous aggregate publishing; synthetic data only, no identity mapping."""
from datetime import datetime, timedelta
import json
import unittest

from reducer.race import validate_summary, encode, RaceValidationError, COMP_FIELDS


NOW = datetime.fromisoformat("2026-10-10T12:00:00-04:00")


def sample():
    return {"version": 2, "week_start": "2026-10-05", "week_end": "2026-10-11",
            "timezone": "America/Toronto", "generated_at": NOW.isoformat(), "a": 7500, "b": 8900,
            "stats": {"v": 1, "d": 20261010, "w": 20261005, "c": [7500, 4, 8900, 5],
                      "p": [5200, 3, 4600, 3], "m": [20000, 12, 18000, 11],
                      "y": [120000, 72, 110000, 69], "l": [640000, 400, 440000, 300],
                      "x": [2000, 2500]}}


class CompetitionTests(unittest.TestCase):
    def test_v2_is_bounded_and_anonymous(self):
        data = validate_summary(sample(), NOW)
        raw = encode(data)
        self.assertEqual(tuple(json.loads(raw)), COMP_FIELDS)
        self.assertLess(len(raw), 1024)
        self.assertEqual(data["stats"]["c"], [7500, 4, 8900, 5])

    def test_legacy_v1_and_carry_are_still_accepted(self):
        data = sample()
        data["version"] = 1
        del data["stats"]
        self.assertEqual(validate_summary(data, NOW), data)
        self.assertEqual(validate_summary(sample(), NOW + timedelta(days=1), timedelta(days=8)), sample())

    def test_unknown_fields_fail_closed_and_never_reach_the_published_artifact(self):
        for target in ("top", "stats"):
            data = sample()
            (data if target == "top" else data["stats"])["private_sessions"] = ["DO NOT PUBLISH"]
            with self.assertRaises(RaceValidationError):
                validate_summary(data, NOW)

    def test_bad_metrics_dates_counts_and_periods_fail_closed(self):
        changes = [("v", True), ("d", 20261009), ("w", 20261006), ("c", [True, 1, 0, 0]),
                   ("c", [7500, 0, 8900, 5]), ("l", [1, 1, 1, 1]), ("x", [7501, 0]),
                   ("m", [1e9, 12, 18000, 11])]
        for key, value in changes:
            data = sample()
            data["stats"][key] = value
            with self.subTest(key=key, value=value), self.assertRaises(RaceValidationError):
                validate_summary(data, NOW)
        data = sample()
        data["a"] += 1
        with self.assertRaises(RaceValidationError):
            validate_summary(data, NOW)


if __name__ == "__main__":
    unittest.main()
