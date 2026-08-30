import copy
from datetime import date, datetime
import json
from pathlib import Path
import tempfile
import unittest
import zoneinfo

from reducer.reducer import (
    SourceValidationError,
    TARGET_FACILITIES,
    build_outputs,
    get_target_dates,
    write_outputs_atomic,
)


BASE_URL = "https://ottawa.ca/en/recreation-and-parks/facilities/place-listing/"
REF_DATE = date(2026, 8, 29)
GENERATED = datetime(2026, 8, 29, 12, 0, tzinfo=zoneinfo.ZoneInfo("America/Toronto"))


def source_data():
    return {
        "facility": [
            {
                "url": BASE_URL + slug,
                "name": name,
                "scrapedAt": "2026-08-29",
            }
            for slug, name in TARGET_FACILITIES.items()
        ],
        "activity": [],
        "html": [],
        "attribution": [
            {"text": "Compiled data © Patrick Gaskin. https://github.com/ottrec/scraper"},
            {"text": "Schedules © City of Ottawa. https://ottawa.ca/"},
        ],
        "error": [],
    }


def activity(
    facility="brewer-pool-and-arena",
    name="Lane Swim - 25m",
    weekday="Saturday",
    start_date="2026-08-01",
    end_date="2026-09-30",
    start_time="10:00",
    end_time="11:00",
    reservation=False,
    exceptions_id=0,
):
    return {
        "facilityUrl": BASE_URL + facility,
        "name": name,
        "weekday": weekday,
        "startDate": start_date,
        "endDate": end_date,
        "startTime": start_time,
        "endTime": end_time,
        "reservationRequired": reservation,
        "exceptionsHtmlId": exceptions_id,
    }


def facility(feed, slug="brewer-pool-and-arena"):
    return next(item for item in feed["facilities"] if item["id"] == slug)


class ReducerRegressionTests(unittest.TestCase):
    def build(self, data):
        return build_outputs(data, REF_DATE, GENERATED)

    def test_target_dates_are_explicit_weekends_in_rolling_14_days(self):
        self.assertEqual(
            get_target_dates(date(2026, 8, 28)),
            [
                date(2026, 8, 29),
                date(2026, 8, 30),
                date(2026, 9, 5),
                date(2026, 9, 6),
            ],
        )

    def test_unambiguous_cancellation_removes_slot_and_no_html_reaches_outputs(self):
        data = source_data()
        data["html"] = [
            {
                "id": 1,
                "html": "<ul><li><strong>Saturday, August 29</strong>"
                "<ul><li>Lane swim, 10 to 11 am, cancelled</li></ul></li></ul>",
            }
        ]
        data["activity"] = [activity(exceptions_id=1)]
        feed, notices = self.build(data)
        brewer = facility(feed)
        self.assertEqual(
            [item["date"] for item in brewer["schedules"]], ["2026-09-05"]
        )
        self.assertEqual(len(brewer["cancellations"]), 1)
        self.assertNotIn("<", json.dumps(feed))
        self.assertNotIn("<", json.dumps(notices))

    def test_variant_specific_cancellation_does_not_remove_another_lane_swim(self):
        data = source_data()
        data["html"] = [
            {
                "id": 3,
                "html": "<p><strong>Saturday, August 29</strong></p>"
                "<p>Lane Swim - 25m, 10 to 11 am, cancelled</p>",
            }
        ]
        data["activity"] = [
            activity(name="Lane Swim - 25m", exceptions_id=3),
            activity(name="Lane Swim - 50m", exceptions_id=3),
        ]
        feed, _ = self.build(data)
        august_sessions = [
            item for item in facility(feed)["schedules"] if item["date"] == "2026-08-29"
        ]
        self.assertEqual([item["name"] for item in august_sessions], ["Lane Swim - 50m"])

    def test_facility_and_pool_closures_remove_all_affected_swims(self):
        data = source_data()
        data["html"] = [
            {
                "id": 2,
                "html": "<h3>Closure</h3><ul><li><strong>August 29 to September 6</strong>"
                "<ul><li>The pools are closed for annual maintenance.</li></ul></li></ul>",
            }
        ]
        data["facility"][0]["specialHoursHtmlId"] = 2
        data["activity"] = [activity(), activity(weekday="Sunday")]
        feed, _ = self.build(data)
        brewer = facility(feed)
        self.assertEqual(brewer["schedules"], [])
        self.assertEqual(
            brewer["closures"][0]["startDate"], "2026-08-29"
        )

    def test_future_start_date_is_enforced_when_end_date_is_missing(self):
        data = source_data()
        data["activity"] = [
            activity(start_date="2026-09-05", end_date=None)
        ]
        feed, _ = self.build(data)
        sessions = facility(feed)["schedules"]
        self.assertEqual([item["date"] for item in sessions], ["2026-09-05"])
        self.assertEqual(sessions[0]["status"], "uncertain")

    def test_capitalized_weekday_is_accepted_but_lane_skating_is_not(self):
        data = source_data()
        data["activity"] = [
            activity(weekday="Saturday"),
            activity(name="Lane skating", weekday="Saturday", start_time="12:00", end_time="13:00"),
            activity(name="Lane Swim - Reduced Capacity", weekday="Saturday", start_time="14:00", end_time="15:00"),
        ]
        feed, _ = self.build(data)
        self.assertEqual(len(facility(feed)["schedules"]), 2)
        self.assertTrue(all(item["name"] == "Lane Swim - 25m" for item in facility(feed)["schedules"]))

    def test_duplicate_slots_merge_different_warning_sets_and_reservation(self):
        data = source_data()
        data["html"] = [
            {"id": 10, "html": "<p>Schedule details are unclear.</p>"},
            {"id": 11, "html": "<p>Call the facility to confirm.</p>"},
        ]
        data["activity"] = [
            activity(exceptions_id=10, reservation=False),
            activity(exceptions_id=11, reservation=True),
        ]
        feed, _ = self.build(data)
        sessions = facility(feed)["schedules"]
        self.assertEqual(len(sessions), 2)  # One merged slot on each Saturday.
        self.assertTrue(sessions[0]["reservationRequired"])
        self.assertEqual(sessions[0]["status"], "uncertain")
        self.assertGreaterEqual(len(sessions[0]["warnings"]), 3)

    def test_missing_start_time_is_a_controlled_validation_failure(self):
        data = source_data()
        data["activity"] = [activity(start_time=None)]
        with self.assertRaisesRegex(SourceValidationError, "startTime"):
            self.build(data)

    def test_invalid_target_dates_and_times_are_rejected(self):
        for field, value, message in (
            ("startDate", "2026-02-30", "real date"),
            ("endTime", "25:00", "real time"),
        ):
            with self.subTest(field=field):
                data = source_data()
                row = activity()
                row[field] = value
                data["activity"] = [row]
                with self.assertRaisesRegex(SourceValidationError, message):
                    self.build(data)

    def test_target_scraper_error_is_rejected_but_unrelated_error_is_not(self):
        data = source_data()
        data["error"] = [
            {"facilityUrl": BASE_URL + "brewer-pool-and-arena", "error": "parse failed"}
        ]
        with self.assertRaisesRegex(SourceValidationError, "target facility"):
            self.build(data)

        data["error"][0]["facilityUrl"] = BASE_URL + "unrelated-centre"
        feed, _ = self.build(data)
        self.assertEqual(feed["validity"]["sourceStatus"], "valid")

    def test_stale_target_source_is_rejected_instead_of_freshly_timestamped(self):
        data = source_data()
        for item in data["facility"]:
            item["scrapedAt"] = "2026-08-26"
        with self.assertRaisesRegex(SourceValidationError, "source is stale"):
            self.build(data)

    def test_empty_object_is_rejected_not_treated_as_no_swims(self):
        with self.assertRaisesRegex(SourceValidationError, "missing required"):
            self.build({})

    def test_valid_source_with_no_swims_is_an_explicit_valid_empty_feed(self):
        feed, _ = self.build(source_data())
        self.assertEqual(feed["validity"]["status"], "valid")
        self.assertEqual(feed["validity"]["sourceStatus"], "valid")
        self.assertEqual(feed["validity"]["sessionCount"], 0)

    def test_ambiguous_dated_notice_marks_apparent_availability_uncertain(self):
        data = source_data()
        data["html"] = [
            {
                "id": 20,
                "html": "<ul><li><strong>Saturday, August 29</strong>"
                "<ul><li>See holiday weekend schedule.</li></ul></li></ul>",
            }
        ]
        data["activity"] = [activity(exceptions_id=20)]
        feed, _ = self.build(data)
        sessions = facility(feed)["schedules"]
        by_date = {item["date"]: item for item in sessions}
        self.assertEqual(by_date["2026-08-29"]["status"], "uncertain")
        self.assertEqual(by_date["2026-09-05"]["status"], "scheduled")
        self.assertEqual(feed["validity"]["status"], "valid_with_uncertainty")

    def test_facility_matching_is_exact_not_a_name_substring(self):
        data = source_data()
        data["facility"].append(
            {
                "url": BASE_URL + "brewer-pool-and-arena-annex",
                "name": "Brewer Pool and Arena Annex",
                "scrapedAt": "2026-08-29",
            }
        )
        data["activity"] = [activity(facility="brewer-pool-and-arena-annex")]
        feed, _ = self.build(data)
        self.assertEqual(feed["validity"]["sessionCount"], 0)

    def test_same_day_source_correction_changes_content_hash(self):
        first = source_data()
        first["activity"] = [activity(start_time="10:00")]
        second = copy.deepcopy(first)
        second["activity"][0]["startTime"] = "10:15"
        feed_one, _ = self.build(first)
        feed_two, _ = self.build(second)
        self.assertEqual(feed_one["generatedAt"], feed_two["generatedAt"])
        self.assertNotEqual(feed_one["contentHash"], feed_two["contentHash"])

    def test_generation_time_does_not_change_content_hash(self):
        data = source_data()
        data["activity"] = [activity()]
        feed_one, _ = build_outputs(data, REF_DATE, GENERATED)
        feed_two, _ = build_outputs(
            data,
            REF_DATE,
            datetime(2026, 8, 29, 18, 0, tzinfo=GENERATED.tzinfo),
        )
        self.assertNotEqual(feed_one["generatedAt"], feed_two["generatedAt"])
        self.assertEqual(feed_one["contentHash"], feed_two["contentHash"])

    def test_metadata_preserves_freshness_and_attribution(self):
        feed, notices = self.build(source_data())
        self.assertEqual(feed["version"], 2)
        self.assertEqual(feed["source"]["freshness"]["newestScrapedAt"], "2026-08-29")
        self.assertEqual(len(feed["source"]["attribution"]), 2)
        self.assertEqual(notices["scheduleContentHash"], feed["contentHash"])

    def test_atomic_writer_replaces_outputs_and_leaves_no_temp_files(self):
        feed, notices = self.build(source_data())
        with tempfile.TemporaryDirectory() as directory:
            schedule_path = Path(directory) / "schedule.json"
            notices_path = Path(directory) / "notices.json"
            schedule_path.write_text("old schedule", encoding="utf-8")
            notices_path.write_text("old notices", encoding="utf-8")
            write_outputs_atomic(schedule_path, notices_path, feed, notices)
            self.assertEqual(json.loads(schedule_path.read_text())["version"], 2)
            self.assertEqual(json.loads(notices_path.read_text())["version"], 1)
            self.assertEqual(list(Path(directory).glob(".*.json.*")), [])


if __name__ == "__main__":
    unittest.main()
