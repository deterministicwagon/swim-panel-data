"""Build a small, defensive weekend lane-swim feed from ottrec data."""

from __future__ import annotations

import argparse
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path
import re
import tempfile
from datetime import date, datetime, time, timedelta
from typing import Any
from urllib.parse import urlparse
import zoneinfo


TIMEZONE = "America/Toronto"
SOURCE_URL = "https://data.ottrec.ca/export/latest.json"
HORIZON_DAYS = 14
TARGET_FACILITIES = {
    "brewer-pool-and-arena": "Brewer Pool and Arena",
    "minto-recreation-complex-barrhaven": "Minto Recreation Complex - Barrhaven",
    "richcraft-recreation-complex-kanata": "Richcraft Recreation Complex-Kanata",
    "nepean-sportsplex": "Nepean Sportsplex",
}
WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
MONTHS = {
    name.lower(): number
    for number, name in enumerate(
        (
            "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December",
        ),
        1,
    )
}
DATE_TOKEN_RE = re.compile(
    r"(?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\s*,?\s*)?"
    r"(" + "|".join(name.title() for name in MONTHS) + r")\s+(\d{1,2})"
    r"(?:\s*,\s*(\d{4}))?",
    re.IGNORECASE,
)
TIME_RANGE_RE = re.compile(
    r"\b(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)?\s*"
    r"(?:to|[-–—])\s*(\d{1,2})(?::(\d{2}))?\s*(a\.?m\.?|p\.?m\.?)\b",
    re.IGNORECASE,
)
ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
ISO_TIME_RE = re.compile(r"^\d{2}:\d{2}$")


class SourceValidationError(ValueError):
    """Raised when publishing the source would risk a misleading feed."""


class _TextExtractor(HTMLParser):
    BLOCK_TAGS = {"br", "div", "h1", "h2", "h3", "h4", "li", "p", "tr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def html_to_text(value: str) -> str:
    """Convert source HTML to compact plain text; HTML never reaches the device."""
    parser = _TextExtractor()
    parser.feed(value)
    lines = []
    for raw_line in "".join(parser.parts).replace("\xa0", " ").splitlines():
        line = " ".join(raw_line.split())
        if line and (not lines or line != lines[-1]):
            lines.append(line)
    return "\n".join(lines)


def hash_data(data: Any) -> str:
    encoded = json.dumps(
        data, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def get_target_dates(ref_date: datetime | date, horizon: int = HORIZON_DAYS) -> list[date]:
    first = ref_date.date() if isinstance(ref_date, datetime) else ref_date
    return [
        first + timedelta(days=offset)
        for offset in range(horizon)
        if (first + timedelta(days=offset)).weekday() in (5, 6)
    ]


def _slug(url: str) -> str:
    return urlparse(url).path.rstrip("/").rsplit("/", 1)[-1].casefold()


def _parse_date(value: Any, field: str, *, allow_none: bool = True) -> date | None:
    if value is None and allow_none:
        return None
    if not isinstance(value, str) or not ISO_DATE_RE.fullmatch(value):
        raise SourceValidationError(f"{field} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SourceValidationError(f"{field} is not a real date: {value!r}") from exc


def _parse_time(value: Any, field: str) -> str:
    if not isinstance(value, str) or not ISO_TIME_RE.fullmatch(value):
        raise SourceValidationError(f"{field} must be a time in HH:MM format")
    try:
        time.fromisoformat(value)
    except ValueError as exc:
        raise SourceValidationError(f"{field} is not a real time: {value!r}") from exc
    return value


def _parse_scraped_at(value: Any, facility_id: str) -> str:
    if not isinstance(value, str) or not value:
        raise SourceValidationError(f"facility {facility_id} has no scrapedAt value")
    candidate = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        datetime.fromisoformat(candidate)
    except ValueError:
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise SourceValidationError(
                f"facility {facility_id} has invalid scrapedAt value {value!r}"
            ) from exc
    return value


def _is_lane_swim(name: Any) -> bool:
    if not isinstance(name, str):
        return False
    normalized = " ".join(name.casefold().split())
    return bool(re.search(r"\blane\s+swim\b", normalized)) and not bool(
        re.search(r"\breduced\s+capacity\b", normalized)
    )


def _resolve_notice_date(month: int, day: int, year: int | None, ref: date) -> date:
    if year is not None:
        try:
            return date(year, month, day)
        except ValueError as exc:
            raise SourceValidationError("notice contains an invalid calendar date") from exc
    candidates = []
    for candidate_year in (ref.year - 1, ref.year, ref.year + 1):
        try:
            candidates.append(date(candidate_year, month, day))
        except ValueError:
            continue
    if not candidates:
        raise SourceValidationError("notice contains an invalid calendar date")
    return min(candidates, key=lambda candidate: abs((candidate - ref).days))


def _notice_range(line: str, ref: date) -> tuple[date, date] | None:
    matches = list(DATE_TOKEN_RE.finditer(line))
    if not matches:
        return None
    first_match = matches[0]
    first = _resolve_notice_date(
        MONTHS[first_match.group(1).casefold()],
        int(first_match.group(2)),
        int(first_match.group(3)) if first_match.group(3) else None,
        ref,
    )
    if len(matches) == 1:
        return first, first
    second_match = matches[1]
    explicit_year = int(second_match.group(3)) if second_match.group(3) else None
    if explicit_year is None:
        second_year = first.year
        second_month = MONTHS[second_match.group(1).casefold()]
        if (second_month, int(second_match.group(2))) < (first.month, first.day):
            second_year += 1
    else:
        second_year = explicit_year
    try:
        second = date(
            second_year,
            MONTHS[second_match.group(1).casefold()],
            int(second_match.group(2)),
        )
    except ValueError as exc:
        raise SourceValidationError("notice contains an invalid calendar date") from exc
    if second < first:
        raise SourceValidationError("notice contains a backwards date range")
    return first, second


def _clock_12h(hour: int, minute: int, marker: str) -> str:
    marker = marker.casefold().replace(".", "")
    if not 1 <= hour <= 12 or not 0 <= minute <= 59:
        raise SourceValidationError("notice contains an invalid time")
    if marker == "am":
        hour = 0 if hour == 12 else hour
    else:
        hour = 12 if hour == 12 else hour + 12
    return f"{hour:02d}:{minute:02d}"


def _notice_time_range(text: str) -> tuple[str, str] | None:
    match = TIME_RANGE_RE.search(text)
    if not match:
        return None
    first_marker = match.group(3) or match.group(6)
    return (
        _clock_12h(int(match.group(1)), int(match.group(2) or 0), first_marker),
        _clock_12h(int(match.group(4)), int(match.group(5) or 0), match.group(6)),
    )


def _cancelled_activity_name(text: str) -> str | None:
    for line in text.splitlines():
        folded = line.casefold()
        if re.search(r"\blane\s+swim\b", folded) and re.search(r"\bcancell?ed\b", folded):
            label = re.split(r",|\b\d{1,2}(?::\d{2})?\b", line, maxsplit=1)[0]
            return " ".join(label.casefold().split()).strip(" -–—")
    return None


def _notice_id(facility_id: str, kind: str, text: str) -> str:
    digest = hashlib.sha256(f"{facility_id}\0{kind}\0{text}".encode("utf-8")).hexdigest()
    return f"n-{digest[:12]}"


def _analyze_notice(
    facility_id: str,
    source_type: str,
    text: str,
    ref: date,
) -> list[dict[str, Any]]:
    """Extract only conservative closure/cancellation/uncertainty rules."""
    lines = text.splitlines()
    rules: list[dict[str, Any]] = []
    dated_lines = [(index, _notice_range(line, ref)) for index, line in enumerate(lines)]
    dated_lines = [(index, span) for index, span in dated_lines if span]

    for position, (line_index, span) in enumerate(dated_lines):
        next_index = dated_lines[position + 1][0] if position + 1 < len(dated_lines) else len(lines)
        context_end = next_index
        if context_end > line_index:
            possible_heading = lines[context_end - 1]
            if (
                len(possible_heading.split()) <= 5
                and not possible_heading.endswith((".", "!", "?"))
                and re.fullmatch(
                    r".*(?:closure|schedule|hours|change)s?.*",
                    possible_heading,
                    re.IGNORECASE,
                )
            ):
                context_end -= 1
        previous = lines[line_index - 1] if line_index else ""
        context_lines = []
        if re.search(r"closure|schedule|hours|change", previous, re.IGNORECASE):
            context_lines.append(previous)
        context_lines.extend(lines[line_index:context_end])
        context = "\n".join(context_lines)
        folded = " ".join(context.casefold().split())
        kind = None
        if re.search(r"\b(?:the\s+)?(?:facility|pool|pools)\s+(?:is|are)\s+closed\b", folded):
            kind = "closure"
        elif re.search(r"\ball\s+(?:swim\s+)?drop-ins?\s+(?:are\s+)?cancell?ed\b", folded):
            kind = "closure"
        elif re.search(r"\blane\s+swim\b", folded) and re.search(r"\bcancell?ed\b", folded):
            kind = "cancellation"
        elif source_type == "exceptions" and re.search(r"\bsee\b.*\bschedule\b", folded):
            kind = "uncertainty"
        elif source_type == "special_hours" and (
            re.search(r"schedule|special\s+hours|weekend\s+hours", folded)
            or _notice_time_range(context)
        ):
            kind = "uncertainty"
        if kind is None:
            continue
        rule: dict[str, Any] = {
            "id": _notice_id(facility_id, kind, context),
            "facilityId": facility_id,
            "type": kind,
            "sourceType": source_type,
            "startDate": span[0].isoformat(),
            "endDate": span[1].isoformat(),
            "text": context,
        }
        if kind == "cancellation":
            times = _notice_time_range(context)
            if times:
                rule["startTime"], rule["endTime"] = times
            activity_name = _cancelled_activity_name(context)
            if activity_name:
                rule["activityName"] = activity_name
        rules.append(rule)

    if source_type == "exceptions" and text and not dated_lines:
        rules.append(
            {
                "id": _notice_id(facility_id, "uncertainty", text),
                "facilityId": facility_id,
                "type": "uncertainty",
                "sourceType": source_type,
                "text": text,
            }
        )
    return rules


def _overlaps_horizon(rule: dict[str, Any], target_dates: list[date]) -> bool:
    if "startDate" not in rule:
        return True
    start = date.fromisoformat(rule["startDate"])
    end = date.fromisoformat(rule["endDate"])
    return any(start <= candidate <= end for candidate in target_dates)


def _rule_applies(rule: dict[str, Any], session: dict[str, Any]) -> bool:
    if "startDate" in rule:
        session_date = date.fromisoformat(session["date"])
        if not (
            date.fromisoformat(rule["startDate"])
            <= session_date
            <= date.fromisoformat(rule["endDate"])
        ):
            return False
    if rule["type"] == "cancellation" and "startTime" in rule:
        if not (
            session["startTime"] == rule["startTime"]
            and session["endTime"] == rule["endTime"]
        ):
            return False
    if rule["type"] == "cancellation" and rule.get("activityName") not in (None, "lane swim"):
        return " ".join(session["name"].casefold().split()) == rule["activityName"]
    return True


def _validate_source(data: Any) -> tuple[
    list[dict[str, Any]], list[dict[str, Any]], dict[Any, str], list[str]
]:
    if not isinstance(data, dict):
        raise SourceValidationError("source root must be a JSON object")
    for key in ("facility", "activity", "html", "attribution", "error"):
        if key not in data:
            raise SourceValidationError(f"source is missing required {key!r} collection")
        if not isinstance(data[key], list):
            raise SourceValidationError(f"source {key!r} must be a list")

    html_map: dict[Any, str] = {}
    for index, item in enumerate(data["html"]):
        if not isinstance(item, dict) or "id" not in item or not isinstance(item.get("html"), str):
            raise SourceValidationError(f"html[{index}] must contain id and string html")
        if item["id"] in html_map:
            raise SourceValidationError(f"duplicate HTML id {item['id']!r}")
        html_map[item["id"]] = item["html"]

    facilities_by_slug: dict[str, list[dict[str, Any]]] = {slug: [] for slug in TARGET_FACILITIES}
    for index, facility in enumerate(data["facility"]):
        if not isinstance(facility, dict):
            raise SourceValidationError(f"facility[{index}] must be an object")
        url = facility.get("url")
        if isinstance(url, str) and _slug(url) in facilities_by_slug:
            facilities_by_slug[_slug(url)].append(facility)
    for slug, matches in facilities_by_slug.items():
        if len(matches) != 1:
            raise SourceValidationError(
                f"expected exactly one target facility {slug!r}; found {len(matches)}"
            )
        facility = matches[0]
        if facility.get("name") != TARGET_FACILITIES[slug]:
            raise SourceValidationError(
                f"target facility {slug!r} has unexpected name {facility.get('name')!r}"
            )
        _parse_scraped_at(facility.get("scrapedAt"), slug)
        for id_field in ("specialHoursHtmlId", "notificationsHtmlId"):
            html_id = facility.get(id_field)
            if html_id not in (None, 0) and html_id not in html_map:
                raise SourceValidationError(f"facility {slug} references missing HTML id {html_id!r}")

    facilities = [facilities_by_slug[slug][0] for slug in TARGET_FACILITIES]
    target_urls = {facility["url"] for facility in facilities}
    for index, error in enumerate(data["error"]):
        if not isinstance(error, dict):
            raise SourceValidationError(f"error[{index}] must be an object")
        if error.get("facilityUrl") in target_urls:
            raise SourceValidationError(
                f"ottrec reported an error for target facility {_slug(error['facilityUrl'])}"
            )

    activities = []
    for index, activity in enumerate(data["activity"]):
        if not isinstance(activity, dict):
            raise SourceValidationError(f"activity[{index}] must be an object")
        if activity.get("facilityUrl") not in target_urls or not _is_lane_swim(activity.get("name")):
            continue
        weekday = activity.get("weekday")
        if not isinstance(weekday, str) or not weekday.strip():
            raise SourceValidationError(f"target activity[{index}] has no weekday")
        weekday_folded = weekday.strip().casefold()
        if weekday_folded not in WEEKDAYS:
            _parse_date(weekday.strip(), f"activity[{index}].weekday", allow_none=False)
        start = _parse_date(activity.get("startDate"), f"activity[{index}].startDate")
        end = _parse_date(activity.get("endDate"), f"activity[{index}].endDate")
        if start and end and start > end:
            raise SourceValidationError(f"target activity[{index}] has backwards date bounds")
        _parse_time(activity.get("startTime"), f"activity[{index}].startTime")
        _parse_time(activity.get("endTime"), f"activity[{index}].endTime")
        if not isinstance(activity.get("reservationRequired", False), bool):
            raise SourceValidationError(
                f"activity[{index}].reservationRequired must be a boolean"
            )
        html_id = activity.get("exceptionsHtmlId")
        if html_id not in (None, 0) and html_id not in html_map:
            raise SourceValidationError(
                f"target activity[{index}] references missing HTML id {html_id!r}"
            )
        activities.append(activity)

    attribution = []
    for index, item in enumerate(data["attribution"]):
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not item["text"].strip():
            raise SourceValidationError(f"attribution[{index}] must contain non-empty text")
        attribution.append(item["text"].strip())
    if not attribution:
        raise SourceValidationError("source attribution must not be empty")
    return facilities, activities, html_map, attribution


def build_outputs(
    data: Any,
    ref_date: datetime | date,
    generated_at: datetime | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    facilities, activities, html_map, attribution = _validate_source(data)
    ref = ref_date.date() if isinstance(ref_date, datetime) else ref_date
    target_dates = get_target_dates(ref)
    horizon_end = ref + timedelta(days=HORIZON_DAYS - 1)
    tz = zoneinfo.ZoneInfo(TIMEZONE)
    if generated_at is None:
        generated_at = datetime.now(tz)
    elif generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=tz)
    generated_text = generated_at.isoformat()

    facility_by_url = {facility["url"]: facility for facility in facilities}
    output_by_slug: dict[str, dict[str, Any]] = {}
    all_notice_details: dict[str, dict[str, Any]] = {}
    rules_by_slug: dict[str, list[dict[str, Any]]] = {slug: [] for slug in TARGET_FACILITIES}
    notice_text_by_facility: dict[str, set[tuple[str, str]]] = {
        slug: set() for slug in TARGET_FACILITIES
    }

    for facility in facilities:
        slug = _slug(facility["url"])
        output_by_slug[slug] = {
            "id": slug,
            "name": facility["name"],
            "scrapedAt": facility["scrapedAt"],
            "closures": [],
            "cancellations": [],
            "warnings": [],
            "schedules": [],
        }
        for field, source_type in (
            ("specialHoursHtmlId", "special_hours"),
            ("notificationsHtmlId", "notifications"),
        ):
            html_id = facility.get(field)
            if html_id not in (None, 0):
                text = html_to_text(html_map[html_id])
                if text:
                    notice_text_by_facility[slug].add((source_type, text))

    activity_source_notice: dict[int, str | None] = {}
    for activity in activities:
        slug = _slug(activity["facilityUrl"])
        html_id = activity.get("exceptionsHtmlId")
        activity_source_notice[id(activity)] = None
        if html_id not in (None, 0):
            text = html_to_text(html_map[html_id])
            if text:
                notice_text_by_facility[slug].add(("exceptions", text))
                activity_source_notice[id(activity)] = text

    for slug, source_notices in notice_text_by_facility.items():
        for source_type, text in sorted(source_notices):
            source_id = _notice_id(slug, "source_notice", text)
            all_notice_details[source_id] = {
                "id": source_id,
                "facilityId": slug,
                "type": "information",
                "sourceType": source_type,
                "text": text,
            }
            for rule in _analyze_notice(slug, source_type, text, ref):
                if _overlaps_horizon(rule, target_dates):
                    rule["sourceText"] = text
                    rules_by_slug[slug].append(rule)
                    detail = rule.copy()
                    detail.pop("sourceText")
                    all_notice_details[rule["id"]] = detail

    for activity in activities:
        facility = facility_by_url[activity["facilityUrl"]]
        slug = _slug(facility["url"])
        weekday_raw = activity["weekday"].strip()
        weekday = weekday_raw.casefold()
        start_bound = _parse_date(activity.get("startDate"), "startDate")
        end_bound = _parse_date(activity.get("endDate"), "endDate")
        missing_bounds = []
        if start_bound is None:
            missing_bounds.append("startDate")
        if end_bound is None:
            missing_bounds.append("endDate")

        for candidate in target_dates:
            if weekday in WEEKDAYS:
                if candidate.weekday() != WEEKDAYS[weekday]:
                    continue
            elif candidate != date.fromisoformat(weekday_raw):
                continue
            if start_bound is not None and candidate < start_bound:
                continue
            if end_bound is not None and candidate > end_bound:
                continue

            session: dict[str, Any] = {
                "date": candidate.isoformat(),
                "name": " ".join(activity["name"].split()),
                "startTime": activity["startTime"],
                "endTime": activity["endTime"],
                "reservationRequired": activity.get("reservationRequired", False),
                "status": "scheduled",
                "warnings": [],
            }
            if missing_bounds:
                text = (
                    f"The source activity {session['name']!r} omits "
                    + " and ".join(missing_bounds)
                    + "; the known boundary was still enforced."
                )
                warning_id = _notice_id(slug, "uncertainty", text)
                session["warnings"].append(warning_id)
                session["status"] = "uncertain"
                all_notice_details[warning_id] = {
                    "id": warning_id,
                    "facilityId": slug,
                    "type": "uncertainty",
                    "sourceType": "activity_bounds",
                    "text": text,
                }

            removed = False
            for rule in rules_by_slug[slug]:
                if rule["sourceType"] == "exceptions" and (
                    rule["sourceText"] != activity_source_notice[id(activity)]
                ):
                    continue
                if not _rule_applies(rule, session):
                    continue
                if rule["type"] == "closure":
                    removed = True
                    break
                if rule["type"] == "cancellation":
                    output_by_slug[slug]["cancellations"].append(
                        {
                            "date": session["date"],
                            "name": session["name"],
                            "startTime": session["startTime"],
                            "endTime": session["endTime"],
                            "warning": rule["id"],
                        }
                    )
                    removed = True
                    break
                if rule["type"] == "uncertainty":
                    session["status"] = "uncertain"
                    session["warnings"].append(rule["id"])
                    output_by_slug[slug]["warnings"].append(rule["id"])
            if not removed:
                output_by_slug[slug]["schedules"].append(session)

    for slug, rules in rules_by_slug.items():
        output = output_by_slug[slug]
        for rule in rules:
            if rule["type"] == "closure":
                output["closures"].append(
                    {
                        "startDate": rule["startDate"],
                        "endDate": rule["endDate"],
                        "warning": rule["id"],
                    }
                )
            elif rule["type"] == "uncertainty":
                output["warnings"].append(rule["id"])

        merged: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for session in output["schedules"]:
            key = (
                session["date"], session["startTime"], session["endTime"],
                session["name"].casefold(),
            )
            if key not in merged:
                merged[key] = session
                continue
            current = merged[key]
            if current["reservationRequired"] != session["reservationRequired"]:
                text = "Duplicate source rows disagree about whether reservation is required; true was retained."
                warning_id = _notice_id(slug, "uncertainty", text)
                current["warnings"].append(warning_id)
                current["status"] = "uncertain"
                all_notice_details[warning_id] = {
                    "id": warning_id,
                    "facilityId": slug,
                    "type": "uncertainty",
                    "sourceType": "duplicate_merge",
                    "text": text,
                }
            current["reservationRequired"] = (
                current["reservationRequired"] or session["reservationRequired"]
            )
            current["warnings"] = sorted(set(current["warnings"] + session["warnings"]))
            if current["warnings"]:
                current["status"] = "uncertain"

        output["schedules"] = sorted(
            merged.values(),
            key=lambda item: (
                item["date"], item["startTime"], item["endTime"], item["name"].casefold()
            ),
        )
        for session in output["schedules"]:
            session["warnings"] = sorted(set(session["warnings"]))
        output["closures"] = sorted(
            {json.dumps(item, sort_keys=True): item for item in output["closures"]}.values(),
            key=lambda item: (item["startDate"], item["endDate"], item["warning"]),
        )
        output["cancellations"] = sorted(
            {json.dumps(item, sort_keys=True): item for item in output["cancellations"]}.values(),
            key=lambda item: (item["date"], item["startTime"], item["endTime"], item["name"]),
        )
        output["warnings"] = sorted(set(output["warnings"]))

    output_facilities = [output_by_slug[slug] for slug in TARGET_FACILITIES]
    session_count = sum(len(item["schedules"]) for item in output_facilities)
    uncertain_count = sum(
        session["status"] == "uncertain"
        for facility in output_facilities for session in facility["schedules"]
    )
    has_uncertainty = uncertain_count > 0 or any(
        facility["warnings"] for facility in output_facilities
    )
    relevant_source = {
        "facilities": facilities,
        "activities": activities,
        "notices": sorted(
            [list(item) for notices in notice_text_by_facility.values() for item in notices]
        ),
        "attribution": attribution,
    }
    scraped_values = sorted(facility["scrapedAt"] for facility in facilities)
    stable_payload: dict[str, Any] = {
        "version": 2,
        "validity": {
            "status": "valid_with_uncertainty" if has_uncertainty else "valid",
            "sourceStatus": "valid",
            "horizon": {
                "startDate": ref.isoformat(),
                "endDate": horizon_end.isoformat(),
                "days": HORIZON_DAYS,
                "timezone": TIMEZONE,
            },
            "sessionCount": session_count,
            "uncertainSessionCount": uncertain_count,
        },
        "source": {
            "url": SOURCE_URL,
            "revisionHash": hash_data(relevant_source),
            "freshness": {
                "oldestScrapedAt": scraped_values[0],
                "newestScrapedAt": scraped_values[-1],
            },
            "attribution": attribution,
        },
        "facilities": output_facilities,
    }
    content_hash = hash_data(stable_payload)
    main = {
        "version": stable_payload["version"],
        "generatedAt": generated_text,
        "contentHash": content_hash,
        **{key: value for key, value in stable_payload.items() if key != "version"},
    }
    notices = {
        "version": 1,
        "generatedAt": generated_text,
        "scheduleContentHash": content_hash,
        "sourceRevisionHash": stable_payload["source"]["revisionHash"],
        "notices": sorted(all_notice_details.values(), key=lambda item: item["id"]),
    }
    return main, notices


def load_source(path: str | os.PathLike[str]) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            return json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise SourceValidationError(f"could not read valid source JSON: {exc}") from exc


def _prepare_json(path: Path, data: Any, pretty: bool) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(
                data, stream, ensure_ascii=False,
                indent=2 if pretty else None,
                separators=None if pretty else (",", ":"),
            )
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return temp_path


def write_outputs_atomic(
    output_path: str | os.PathLike[str],
    notices_path: str | os.PathLike[str],
    main: dict[str, Any],
    notices: dict[str, Any],
    *,
    pretty: bool = False,
) -> None:
    output = Path(output_path)
    notice_output = Path(notices_path)
    if output.resolve() == notice_output.resolve():
        raise ValueError("schedule and notices output paths must differ")
    prepared: list[tuple[Path, Path]] = []
    try:
        prepared.append((_prepare_json(output, main, pretty), output))
        prepared.append((_prepare_json(notice_output, notices, pretty), notice_output))
        for temporary, destination in prepared:
            os.replace(temporary, destination)
    finally:
        for temporary, _ in prepared:
            temporary.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Reduce ottrec data for the swim panel")
    parser.add_argument("--input", required=True, help="downloaded ottrec latest.json")
    parser.add_argument("--output", required=True, help="device schedule.json output")
    parser.add_argument("--notices-output", required=True, help="plain-text notices companion output")
    parser.add_argument("--ref-date", help="YYYY-MM-DD; defaults to today in America/Toronto")
    parser.add_argument("--pretty", action="store_true", help="pretty-print output instead of compact JSON")
    args = parser.parse_args()

    tz = zoneinfo.ZoneInfo(TIMEZONE)
    try:
        ref = (
            _parse_date(args.ref_date, "--ref-date", allow_none=False)
            if args.ref_date else datetime.now(tz).date()
        )
        feed, notices = build_outputs(load_source(args.input), ref)
        write_outputs_atomic(args.output, args.notices_output, feed, notices, pretty=args.pretty)
    except SourceValidationError as exc:
        parser.exit(2, f"source validation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
