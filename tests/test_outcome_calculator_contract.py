from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.calendar import TradingCalendarV1
from yifei_platform.market_data import MarketDataReaderV1, MarketDataSourceV1
from yifei_platform.outcomes import OutcomeCalculatorV1, OutcomeStatus


class OutcomeCalculatorContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "market.db"
        self.sessions = tuple((date(2026, 6, 1) + timedelta(days=offset)).isoformat() for offset in range(6))
        self._seed(self.db_path)
        calendar = TradingCalendarV1(self.sessions, source_version="fixture-calendar.v1")
        reader = MarketDataReaderV1(MarketDataSourceV1(self.db_path, "fixture-market.v1"))
        self.calculator = OutcomeCalculatorV1(
            calendar=calendar,
            market_data=reader,
            price_basis_version="raw-close-ohLC.v1",
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_calculates_trading_window_returns_and_distinct_risk_metrics(self) -> None:
        result = self.calculator.calculate(
            instrument="000001",
            observation_session=self.sessions[0],
        )
        self.assertEqual([1, 3, 5], [item.window for item in result.windows])
        self.assertEqual([10.0, 30.0, 60.0], [item.return_pct for item in result.windows])
        self.assertTrue(all(item.status is OutcomeStatus.COMPLETE for item in result.windows))
        self.assertEqual(65.0, result.mfe_pct)
        self.assertEqual(-2.0, result.mae_pct)
        self.assertEqual(-7.6923, result.max_drawdown_pct)
        self.assertEqual(self.sessions[5], result.metric_end_session)

    def test_does_not_use_observation_day_high_low_for_post_close_metrics(self) -> None:
        result = self.calculator.calculate(
            instrument="000001",
            observation_session=self.sessions[0],
            windows=(1,),
        )
        self.assertEqual(12.0, result.mfe_pct)
        self.assertEqual(-2.0, result.mae_pct)

    def test_unpublished_window_is_explicit_not_zero(self) -> None:
        result = self.calculator.calculate(
            instrument="000001",
            observation_session=self.sessions[3],
            windows=(1, 3),
        )
        self.assertEqual(OutcomeStatus.COMPLETE, result.windows[0].status)
        self.assertEqual(OutcomeStatus.UNAVAILABLE, result.windows[1].status)
        self.assertIsNone(result.windows[1].return_pct)

    def test_missing_entry_or_intermediate_series_is_explicit(self) -> None:
        missing_entry = self.calculator.calculate(
            instrument="999999",
            observation_session=self.sessions[0],
        )
        self.assertEqual(("entry_close_missing",), missing_entry.reason_codes)
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "DELETE FROM stock_daily WHERE stock_code = '000001' AND trade_date = ?",
                (self.sessions[2],),
            )
        incomplete = self.calculator.calculate(
            instrument="000001",
            observation_session=self.sessions[0],
            windows=(3,),
        )
        self.assertEqual(("metric_series_incomplete",), incomplete.reason_codes)
        self.assertIsNone(incomplete.mfe_pct)

    def test_requires_exact_observation_session_and_positive_windows(self) -> None:
        with self.assertRaises(ValueError):
            self.calculator.calculate(instrument="000001", observation_session="2026-06-07")
        with self.assertRaises(ValueError):
            self.calculator.calculate(instrument="000001", observation_session=self.sessions[0], windows=(0,))

    def _seed(self, path: Path) -> None:
        prices = (
            (100.0, 200.0, 50.0),
            (110.0, 112.0, 98.0),
            (105.0, 111.0, 101.0),
            (130.0, 135.0, 104.0),
            (120.0, 132.0, 115.0),
            (160.0, 165.0, 118.0),
        )
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE stock_daily (
                    stock_code TEXT, stock_name TEXT, trade_date TEXT,
                    open REAL, high REAL, low REAL, close REAL, preclose REAL,
                    volume REAL, amount REAL, pct_chg REAL, turnover REAL, is_st INTEGER,
                    PRIMARY KEY (stock_code, trade_date)
                )
                """
            )
            rows = []
            for session, (close, high, low) in zip(self.sessions, prices):
                rows.append(("000001", "测试", session, close, high, low, close, close, 1, 1, 0, 0, 0))
            connection.executemany("INSERT INTO stock_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
