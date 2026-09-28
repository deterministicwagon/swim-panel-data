"""Validate the weekly race summary and prepare it for Pages as race.json.

The summary comes from a private upstream endpoint whose URL is held as a
workflow secret. It carries two anonymous weekly distance totals, `a` and
`b`, in whole metres for the Monday-Sunday America/Toronto week. This module
never adds meaning to those keys. It accepts exactly the published contract
and rebuilds the output from validated values, so an upstream change cannot
leak extra fields.

It also checks the currently published schedule.json/notices.json pair, so a
race-only refresh can carry that pair forward unchanged.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any
import zoneinfo


TIMEZONE = "America/Toronto"
ZONE = zoneinfo.ZoneInfo(TIMEZONE)
FIELDS = ("version", "week_start", "week_end", "timezone", "generated_at", "a", "b")
MAX_BYTES = 512  # The frame rejects anything larger.
MAX_METRES = 1_000_000
# A fresh upstream summary must have been generated within this window of now.
FRESH_WINDOW = timedelta(minutes=10)
# A carried-forward race.json only needs to be for a plausible, recent week.
CARRY_MAX_AGE = timedelta(days=8)


class RaceValidationError(ValueError):
    pass


def _date(value: Any, field: str) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise RaceValidationError(f"{field} is not YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise RaceValidationError(f"{field} is not a valid date") from error


def _metres(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RaceValidationError(f"{field} is not an integer")
    if not 0 <= value <= MAX_METRES:
        raise RaceValidationError(f"{field} is out of range")
    return value


def validate_summary(data: Any, now: datetime, max_age: timedelta = FRESH_WINDOW) -> dict[str, Any]:
    """Return the canonical summary dict, or raise RaceValidationError."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if not isinstance(data, dict):
        raise RaceValidationError("summary is not an object")
    if set(data) != set(FIELDS):
        # Fail closed: an unexpected field could be data that must not publish.
        raise RaceValidationError("summary fields differ from the contract")
    if data["version"] != 1 or isinstance(data["version"], bool):
        raise RaceValidationError("unsupported version")
    if data["timezone"] != TIMEZONE:
        raise RaceValidationError("unexpected timezone")
    start = _date(data["week_start"], "week_start")
    end = _date(data["week_end"], "week_end")
    if start.weekday() != 0 or end != start + timedelta(days=6):
        raise RaceValidationError("week is not Monday to Sunday")
    generated_text = data["generated_at"]
    if not isinstance(generated_text, str) or len(generated_text) != 25:
        raise RaceValidationError("generated_at is not YYYY-MM-DDTHH:MM:SS+HH:MM")
    try:
        generated = datetime.fromisoformat(generated_text)
    except ValueError as error:
        raise RaceValidationError("generated_at is not ISO 8601") from error
    if generated.tzinfo is None:
        raise RaceValidationError("generated_at has no UTC offset")
    local = generated.astimezone(ZONE)
    if generated.utcoffset() != local.utcoffset():
        raise RaceValidationError("generated_at offset is not America/Toronto")
    if not start <= local.date() <= end:
        raise RaceValidationError("generated_at is outside the week")
    if generated - now > timedelta(minutes=5):
        raise RaceValidationError("generated_at is in the future")
    if now - generated > max_age:
        raise RaceValidationError("summary is stale")
    return {
        "version": 1,
        "week_start": start.isoformat(),
        "week_end": end.isoformat(),
        "timezone": TIMEZONE,
        "generated_at": generated_text,
        "a": _metres(data["a"], "a"),
        "b": _metres(data["b"], "b"),
    }


def encode(summary: dict[str, Any]) -> bytes:
    payload = json.dumps(
        {key: summary[key] for key in FIELDS}, separators=(",", ":")
    ).encode("utf-8")
    if len(payload) > MAX_BYTES:
        raise RaceValidationError("race.json exceeds the device size limit")
    return payload


def load_bounded(path: Path, limit: int) -> Any:
    raw = path.read_bytes()
    if not raw or len(raw) > limit:
        raise RaceValidationError(f"{path.name} is empty or too large")
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RaceValidationError(f"{path.name} is not JSON") from error


def check_published_feeds(schedule: Any, notices: Any) -> None:
    """The live schedule/notices pair is safe to carry forward unchanged."""
    if not isinstance(schedule, dict) or schedule.get("version") != 2:
        raise RaceValidationError("published schedule.json is not version 2")
    if schedule.get("validity", {}).get("sourceStatus") != "valid":
        raise RaceValidationError("published schedule.json lacks valid source status")
    content_hash = schedule.get("contentHash")
    if not isinstance(content_hash, str) or len(content_hash) != 64:
        raise RaceValidationError("published schedule.json has no content hash")
    if not isinstance(notices, dict) or notices.get("version") != 1:
        raise RaceValidationError("published notices.json is not version 1")
    if notices.get("scheduleContentHash") != content_hash:
        raise RaceValidationError("published schedule and notices do not match")


def write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
        os.chmod(temp_name, 0o644)
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    fresh = commands.add_parser("summary", help="validate a fresh upstream summary")
    carry = commands.add_parser("carry", help="re-check the published race.json")
    for command in (fresh, carry):
        command.add_argument("--input", required=True, type=Path)
        command.add_argument("--output", required=True, type=Path)
        command.add_argument("--now", help="ISO time with offset, for tests")
    feeds = commands.add_parser("feeds", help="check the published schedule/notices pair")
    feeds.add_argument("--schedule", required=True, type=Path)
    feeds.add_argument("--notices", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "feeds":
            check_published_feeds(
                load_bounded(args.schedule, 512 * 1024),
                load_bounded(args.notices, 512 * 1024),
            )
            print("published schedule/notices pair is valid")
            return 0
        now = datetime.fromisoformat(args.now) if args.now else datetime.now(ZONE)
        max_age = FRESH_WINDOW if args.command == "summary" else CARRY_MAX_AGE
        summary = validate_summary(load_bounded(args.input, 4096), now, max_age)
        write_atomic(args.output, encode(summary))
    except RaceValidationError as error:
        # The message names a rule, never a value from the payload.
        print(f"race summary rejected: {error}", file=sys.stderr)
        return 1
    print(f"race.json written for week {summary['week_start']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
