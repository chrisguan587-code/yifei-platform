from __future__ import annotations

from pathlib import Path
import json
import tempfile
import unittest

from yifei_platform.exchange_calendar_publish import (
    build_exchange_calendar_manifest_v1,
    write_exchange_calendar_manifest_v1,
)


RESOURCE = (
    Path(__file__).parents[1]
    / "resources/exchange_calendar/sse-2026-holiday-closure-source.v1.json"
)

SOURCE = {
    "schema_version": "sse-holiday-closure-source.v1",
    "source_version": "sse-2026-holiday-notice.2025-45.v1",
    "source_ref": (
        "https://www.sse.com.cn/disclosure/announcement/general/"
        "c/c_20251222_10802507.shtml"
    ),
    "coverage_start": "2026-01-01",
    "coverage_end": "2026-12-31",
    "closed_ranges": [
        ["2026-01-01", "2026-01-03"],
        ["2026-02-15", "2026-02-23"],
        ["2026-04-04", "2026-04-06"],
        ["2026-05-01", "2026-05-05"],
        ["2026-06-19", "2026-06-21"],
        ["2026-09-25", "2026-09-27"],
        ["2026-10-01", "2026-10-07"],
    ],
}


class ExchangeCalendarPublishTest(unittest.TestCase):
    def test_repository_source_matches_the_frozen_official_schedule(self) -> None:
        source = json.loads(RESOURCE.read_text(encoding="utf-8"))
        self.assertEqual(SOURCE, source)

    def test_builds_explicit_full_coverage_session_manifest(self) -> None:
        payload = build_exchange_calendar_manifest_v1(
            source=SOURCE,
            published_at="2026-08-09T08:00:00+08:00",
        )

        self.assertEqual("exchange-trading-calendar.v1", payload["schema_version"])
        self.assertIn("2026-08-10", payload["sessions"])
        self.assertIn("2026-10-08", payload["sessions"])
        self.assertNotIn("2026-08-09", payload["sessions"])
        self.assertNotIn("2026-10-01", payload["sessions"])
        self.assertEqual(len(payload["sessions"]), len(set(payload["sessions"])))

    def test_rejects_bad_source_or_naive_publish_time(self) -> None:
        with self.assertRaises(ValueError):
            build_exchange_calendar_manifest_v1(
                source={**SOURCE, "source_ref": ""},
                published_at="2026-08-09T08:00:00+08:00",
            )
        with self.assertRaises(ValueError):
            build_exchange_calendar_manifest_v1(
                source=SOURCE,
                published_at="2026-08-09T08:00:00",
            )
        with self.assertRaises(ValueError):
            build_exchange_calendar_manifest_v1(
                source={**SOURCE, "coverage_start": 20260101},
                published_at="2026-08-09T08:00:00+08:00",
            )
        with self.assertRaises(ValueError):
            build_exchange_calendar_manifest_v1(
                source={**SOURCE, "coverage_start": "2026-06-01"},
                published_at="2026-08-09T08:00:00+08:00",
            )
        with self.assertRaises(ValueError):
            build_exchange_calendar_manifest_v1(
                source=SOURCE,
                published_at=None,  # type: ignore[arg-type]
            )

    def test_atomic_writer_is_idempotent_and_rejects_different_content(self) -> None:
        payload = build_exchange_calendar_manifest_v1(
            source=SOURCE,
            published_at="2026-08-09T08:00:00+08:00",
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "exchange-calendar.json"
            self.assertFalse(
                write_exchange_calendar_manifest_v1(payload=payload, target=target)
            )
            first = target.read_bytes()
            self.assertTrue(
                write_exchange_calendar_manifest_v1(payload=payload, target=target)
            )
            self.assertEqual(first, target.read_bytes())
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "published_at": "2026-08-09T09:00:00+08:00"},
                    target=target,
                )
            self.assertEqual(payload, json.loads(target.read_text(encoding="utf-8")))

    def test_writer_rejects_unsupported_producer_or_source_schema(self) -> None:
        payload = build_exchange_calendar_manifest_v1(
            source=SOURCE,
            published_at="2026-08-09T08:00:00+08:00",
        )
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "exchange-calendar.json"
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "producer_version": "other"},
                    target=target,
                )
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "source_schema_version": "other"},
                    target=target,
                )
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "coverage_start": 20260101},
                    target=target,
                )
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "coverage_end": "2027-12-31"},
                    target=target,
                )
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "unexpected": "value"},
                    target=target,
                )
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "coverage_start": "20260101"},
                    target=target,
                )
            with self.assertRaises(ValueError):
                write_exchange_calendar_manifest_v1(
                    payload={**payload, "sessions": sorted([*payload["sessions"], "2026-08-09"])},
                    target=target,
                )


if __name__ == "__main__":
    unittest.main()
