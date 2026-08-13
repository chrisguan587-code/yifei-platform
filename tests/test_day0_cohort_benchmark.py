from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from yifei_platform import (
    AShareDay0CohortBenchmarkV1,
    MarketDataReaderV1,
    MarketDataSourceV1,
    TradingCalendarV1,
)


class AShareDay0CohortBenchmarkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.database = Path(self.temporary.name) / "market.sqlite3"
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                """CREATE TABLE stock_daily (
                       stock_code TEXT NOT NULL,
                       stock_name TEXT,
                       trade_date TEXT NOT NULL,
                       open REAL, high REAL, low REAL, close REAL, preclose REAL,
                       volume REAL, amount REAL, pct_chg REAL, turnover REAL,
                       is_st INTEGER
                   )"""
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_uses_fixed_day0_pool_and_member_level_cumulative_returns(self) -> None:
        self._rows("2026-08-03", (("000001", 10.0, 0.0), ("600001", 20.0, 0.0)))
        self._rows("2026-08-04", (
            ("000001", 11.0, 10.0), ("600001", 18.0, -10.0),
            ("300001", 30.0, 5.0),  # listed/present only after Day0
        ))
        result = self._calculator().calculate(day0="2026-08-03", as_of="2026-08-04")

        self.assertEqual("ok", result.status)
        self.assertEqual(("000001", "600001"), result.cohort_members)
        self.assertEqual(2, result.cohort_count)
        self.assertEqual(2, result.comparable_count)
        self.assertAlmostEqual(0.0, result.median_return_pct, places=6)
        self.assertAlmostEqual(1.0, result.coverage, places=6)

    def test_coverage_below_95_percent_makes_benchmark_unknown(self) -> None:
        day0 = tuple((f"{index:06d}", 10.0, 0.0) for index in range(100))
        current = tuple((f"{index:06d}", 10.1, 1.0) for index in range(94))
        self._rows("2026-08-03", day0)
        self._rows("2026-08-04", current)

        result = self._calculator().calculate(day0="2026-08-03", as_of="2026-08-04")

        self.assertEqual("data_unknown", result.status)
        self.assertIsNone(result.median_return_pct)
        self.assertEqual("market_return_coverage_below_95pct", result.reason_code)
        self.assertAlmostEqual(0.94, result.coverage, places=6)

    def test_day0_read_failure_is_not_reported_as_an_empty_real_cohort(self) -> None:
        missing = Path(self.temporary.name) / "missing.sqlite3"
        calculator = AShareDay0CohortBenchmarkV1(
            market_data=MarketDataReaderV1(
                MarketDataSourceV1(missing, "missing-market.v1")
            ),
            calendar=TradingCalendarV1(
                ("2026-08-03", "2026-08-04"), source_version="fixture-calendar.v1"
            ),
        )
        result = calculator.calculate(day0="2026-08-03", as_of="2026-08-04")
        self.assertEqual("day0_market_read_failed", result.reason_code)

    def test_non_finite_market_values_cannot_enter_the_benchmark(self) -> None:
        self._rows("2026-08-03", (("000001", 10.0, 0.0), ("600001", 20.0, 0.0)))
        self._rows("2026-08-04", (("000001", 11.0, float("nan")), ("600001", 20.2, 1.0)))
        result = self._calculator().calculate(day0="2026-08-03", as_of="2026-08-04")
        self.assertEqual("data_unknown", result.status)
        self.assertIsNone(result.median_return_pct)

    def test_day0_pool_does_not_apply_v4_liquidity_or_board_filters(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                """INSERT INTO stock_daily
                   (stock_code, stock_name, trade_date, close, volume, amount, pct_chg)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ("688001", "STAR", "2026-08-03", 10.0, 100.0, 1_000.0, 0.0),
                    ("688001", "STAR", "2026-08-04", 10.1, 100.0, 1_000.0, 1.0),
                    ("000001", "MAIN", "2026-08-03", 10.0, 100.0, 1_000.0, 0.0),
                    ("000001", "MAIN", "2026-08-04", 10.1, 100.0, 1_000.0, 1.0),
                ),
            )
        result = self._calculator().calculate(day0="2026-08-03", as_of="2026-08-04")
        self.assertEqual(("000001", "688001"), result.cohort_members)

    def test_confirmed_no_trade_member_can_rejoin_after_reliable_trading_resumes(self) -> None:
        self._rows("2026-08-03", (("000001", 10.0, 0.0), ("600001", 20.0, 0.0)))
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                """INSERT INTO stock_daily
                   (stock_code, stock_name, trade_date, close, volume, amount, pct_chg)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    ("000001", "A", "2026-08-04", 10.0, 0.0, 0.0, None),
                    ("600001", "B", "2026-08-04", 20.2, 100.0, 1_000.0, 1.0),
                    ("000001", "A", "2026-08-05", 10.5, 100.0, 1_000.0, 5.0),
                    ("600001", "B", "2026-08-05", 20.4, 100.0, 1_000.0, 1.0),
                ),
            )
        calculator = AShareDay0CohortBenchmarkV1(
            market_data=MarketDataReaderV1(
                MarketDataSourceV1(self.database, "fixture-market.v1")
            ),
            calendar=TradingCalendarV1(
                ("2026-08-03", "2026-08-04", "2026-08-05"),
                source_version="fixture-calendar.v1",
            ),
        )
        suspended_day = calculator.calculate(day0="2026-08-03", as_of="2026-08-04")
        resumed_day = calculator.calculate(day0="2026-08-03", as_of="2026-08-05")

        self.assertEqual(1, suspended_day.comparable_count)
        self.assertEqual(2, resumed_day.comparable_count)
        self.assertEqual("ok", resumed_day.status)

    def _calculator(self) -> AShareDay0CohortBenchmarkV1:
        return AShareDay0CohortBenchmarkV1(
            market_data=MarketDataReaderV1(
                MarketDataSourceV1(self.database, "fixture-market.v1")
            ),
            calendar=TradingCalendarV1(
                ("2026-08-03", "2026-08-04"), source_version="fixture-calendar.v1"
            ),
        )

    def _rows(self, trade_date: str, rows: tuple[tuple[str, float, float], ...]) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.executemany(
                """INSERT INTO stock_daily
                   (stock_code, stock_name, trade_date, close, volume, amount, pct_chg)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                tuple(
                    (code, code, trade_date, close, 100.0, 1_000.0, pct)
                    for code, close, pct in rows
                ),
            )


if __name__ == "__main__":
    unittest.main()
