from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
from uuid import uuid4


EXCHANGE_CALENDAR_SCHEMA_VERSION = "exchange-trading-calendar.v1"
EXCHANGE_CALENDAR_PUBLISHER_VERSION = "exchange-calendar-publisher.v1"
EXCHANGE_CALENDAR_SOURCE_SCHEMA_VERSION = "sse-holiday-closure-source.v1"


def build_exchange_calendar_manifest_v1(
    *, source: Mapping[str, object], published_at: str,
) -> dict[str, object]:
    """Compile one official annual closure notice into explicit sessions."""
    if source.get("schema_version") != EXCHANGE_CALENDAR_SOURCE_SCHEMA_VERSION:
        raise ValueError("unsupported exchange calendar source schema")
    source_version = _required_text(source.get("source_version"), "source_version")
    source_ref = _required_text(source.get("source_ref"), "source_ref")
    if not isinstance(published_at, str):
        raise ValueError("published_at must be an ISO timestamp string")
    parsed_published_at = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
    if parsed_published_at.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    coverage_start = _iso_date(source.get("coverage_start"), "coverage_start")
    coverage_end = _iso_date(source.get("coverage_end"), "coverage_end")
    _validate_annual_coverage(coverage_start, coverage_end)

    raw_ranges = source.get("closed_ranges")
    if not isinstance(raw_ranges, list):
        raise ValueError("closed_ranges must be a list")
    closed_dates: set[date] = set()
    for raw_range in raw_ranges:
        if not isinstance(raw_range, list) or len(raw_range) != 2:
            raise ValueError("each closed range requires start and end")
        start = _iso_date(raw_range[0], "closed_range_start")
        end = _iso_date(raw_range[1], "closed_range_end")
        if start > end or start < coverage_start or end > coverage_end:
            raise ValueError("closed range is outside calendar coverage")
        for current in _date_range(start, end):
            if current in closed_dates:
                raise ValueError("closed ranges overlap")
            closed_dates.add(current)

    sessions = tuple(
        current.isoformat()
        for current in _date_range(coverage_start, coverage_end)
        if current.weekday() < 5 and current not in closed_dates
    )
    if not sessions:
        raise ValueError("exchange calendar has no trading sessions")
    return {
        "schema_version": EXCHANGE_CALENDAR_SCHEMA_VERSION,
        "producer_version": EXCHANGE_CALENDAR_PUBLISHER_VERSION,
        "source_schema_version": EXCHANGE_CALENDAR_SOURCE_SCHEMA_VERSION,
        "source_version": source_version,
        "source_ref": source_ref,
        "published_at": published_at,
        "coverage_start": coverage_start.isoformat(),
        "coverage_end": coverage_end.isoformat(),
        "sessions": list(sessions),
    }


def write_exchange_calendar_manifest_v1(
    *, payload: Mapping[str, object], target: Path,
) -> bool:
    """Publish one immutable canonical manifest without replacing a peer writer."""
    _validate_manifest(payload)
    encoded = json.dumps(
        dict(payload), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if target.read_bytes() != encoded:
            raise ValueError("exchange calendar target already has different content")
        return True
    temporary = target.parent / f".{target.name}.{uuid4().hex}.tmp"
    try:
        with temporary.open("xb") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, target)
        except FileExistsError:
            if target.read_bytes() != encoded:
                raise ValueError(
                    "exchange calendar target was concurrently published with different content"
                )
            return True
        return False
    finally:
        temporary.unlink(missing_ok=True)


def _validate_manifest(payload: Mapping[str, object]) -> None:
    expected_keys = {
        "schema_version",
        "producer_version",
        "source_schema_version",
        "source_version",
        "source_ref",
        "published_at",
        "coverage_start",
        "coverage_end",
        "sessions",
    }
    if set(payload) != expected_keys:
        raise ValueError(
            "exchange calendar manifest has unexpected or missing fields"
        )
    if payload.get("schema_version") != EXCHANGE_CALENDAR_SCHEMA_VERSION:
        raise ValueError("unsupported exchange calendar manifest schema")
    if payload.get("producer_version") != EXCHANGE_CALENDAR_PUBLISHER_VERSION:
        raise ValueError("unsupported exchange calendar publisher version")
    if payload.get("source_schema_version") != EXCHANGE_CALENDAR_SOURCE_SCHEMA_VERSION:
        raise ValueError("unsupported exchange calendar source schema")
    _required_text(payload.get("source_version"), "source_version")
    _required_text(payload.get("source_ref"), "source_ref")
    raw_published_at = payload.get("published_at")
    if not isinstance(raw_published_at, str):
        raise ValueError("published_at must be an ISO timestamp string")
    parsed = datetime.fromisoformat(raw_published_at.replace("Z", "+00:00"))
    if parsed.utcoffset() is None:
        raise ValueError("published_at must include a timezone")
    start = _iso_date(payload.get("coverage_start"), "coverage_start")
    end = _iso_date(payload.get("coverage_end"), "coverage_end")
    _validate_annual_coverage(start, end)
    raw_sessions = payload.get("sessions")
    if not isinstance(raw_sessions, list) or not raw_sessions:
        raise ValueError("exchange calendar sessions are required")
    sessions = tuple(_iso_date(item, "session") for item in raw_sessions)
    if len(sessions) != len(set(sessions)) or tuple(sorted(sessions)) != sessions:
        raise ValueError("exchange calendar sessions must be sorted and unique")
    if any(item < start or item > end for item in sessions):
        raise ValueError("exchange calendar session is outside coverage")
    if any(item.weekday() >= 5 for item in sessions):
        raise ValueError("exchange calendar sessions must be weekdays")


def _required_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} is required")
    return value


def _iso_date(value: object, field: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO date string")
    parsed = date.fromisoformat(value)
    if value != parsed.isoformat():
        raise ValueError(f"{field} must be a canonical ISO date string")
    return parsed


def _validate_annual_coverage(start: date, end: date) -> None:
    if (
        start.year != end.year
        or start != date(start.year, 1, 1)
        or end != date(start.year, 12, 31)
    ):
        raise ValueError("exchange calendar must cover one complete calendar year")


def _date_range(start: date, end: date):
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)
