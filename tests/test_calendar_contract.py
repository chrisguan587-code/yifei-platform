from __future__ import annotations

from datetime import date, datetime
import json
from pathlib import Path
import unittest

from yifei_platform.calendar import CalendarRangeError, TradingCalendarV1


FIXTURE = Path(__file__).parent / "fixtures" / "market_contract_v1.json"


class TradingCalendarContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        cls.calendar = TradingCalendarV1(
            payload["sessions"],
            source_version=payload["calendar_source_version"],
        )

    def test_resolves_non_session_to_previous_published_session(self) -> None:
        self.assertEqual(date(2026, 6, 5), self.calendar.resolve_session("2026-06-07"))
        self.assertFalse(self.calendar.is_session("2026-06-07"))

    def test_offset_uses_trading_sessions_not_calendar_days(self) -> None:
        self.assertEqual(date(2026, 6, 5), self.calendar.offset_session("2026-06-03", 1))
        self.assertEqual(date(2026, 6, 8), self.calendar.offset_session("2026-06-07", 1))

    def test_context_is_versioned_and_accepts_datetime(self) -> None:
        context = self.calendar.context(datetime(2026, 6, 5, 15, 30))
        self.assertEqual(date(2026, 6, 3), context.previous_session)
        self.assertEqual(date(2026, 6, 8), context.next_session)
        self.assertTrue(context.is_session)
        self.assertEqual("fixture-calendar.2026-06.v1", context.source_version)
        self.assertEqual("trading-calendar.v1", context.schema_version)

    def test_out_of_range_is_explicit(self) -> None:
        with self.assertRaises(CalendarRangeError):
            self.calendar.resolve_session("2026-06-01")
        with self.assertRaises(CalendarRangeError):
            self.calendar.offset_session("2026-06-08", 1)

    def test_requires_source_version_and_sessions(self) -> None:
        with self.assertRaises(ValueError):
            TradingCalendarV1([], source_version="v1")
        with self.assertRaises(ValueError):
            TradingCalendarV1(["2026-06-05"], source_version="")
