from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.calendar import TradingCalendarV1
from yifei_platform.market_data import MarketDataReaderV1, MarketDataSourceV1
from yifei_platform.outcomes import (
    OUTCOME_BATCH_VERSION,
    OutcomeBatchResultV1,
    OutcomeResultV1,
    OutcomeCalculatorV1,
    OutcomeRequestV1,
    OutcomeStatus,
    PriceLineageGuardV1,
    PriceLineageGuardV2,
)


class _CountingReader:
    def __init__(self, reader):
        self._reader = reader
        self.calls = []

    def read_stock_daily(self, as_of: str):
        self.calls.append(as_of)
        return self._reader.read_stock_daily(as_of)


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

    def test_additive_lineage_sources_preserve_old_positional_schema_argument(self) -> None:
        result = OutcomeResultV1(
            "000001", self.sessions[0], 10.0, "raw.v1", (), None,
            None, None, None, (), None, "outcome-calculator.custom.v1",
        )

        self.assertEqual("outcome-calculator.custom.v1", result.schema_version)
        self.assertEqual((), result.price_lineage_sources)

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

    def test_unmatured_window_is_pending_when_cutoff_is_explicit(self) -> None:
        result = self.calculator.calculate(
            instrument="000001",
            observation_session=self.sessions[3],
            windows=(1, 3),
            outcome_as_of=self.sessions[4],
        )
        self.assertEqual(OutcomeStatus.COMPLETE, result.windows[0].status)
        self.assertEqual(OutcomeStatus.PENDING, result.windows[1].status)
        self.assertEqual(("target_session_pending",), result.windows[1].reason_codes)

    def test_price_lineage_guard_rejects_discontinuous_window(self) -> None:
        guarded = OutcomeCalculatorV1(
            calendar=TradingCalendarV1(
                self.sessions, source_version="fixture-calendar.v1"
            ),
            market_data=MarketDataReaderV1(
                MarketDataSourceV1(self.db_path, "fixture-market.v1")
            ),
            price_basis_version="raw-close-ohLC.v1",
            price_lineage_guard=PriceLineageGuardV1(),
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE stock_daily SET preclose=50 WHERE trade_date=?",
                (self.sessions[2],),
            )

        result = guarded.calculate(
            instrument="000001",
            observation_session=self.sessions[0],
            windows=(1, 3),
            outcome_as_of=self.sessions[5],
        )

        self.assertEqual(OutcomeStatus.COMPLETE, result.windows[0].status)
        self.assertEqual(OutcomeStatus.UNAVAILABLE, result.windows[1].status)
        self.assertEqual(
            ("corporate_action_or_price_lineage_discontinuity",),
            result.windows[1].reason_codes,
        )
        self.assertEqual(
            "price-lineage-guard.candidate.v1",
            result.price_lineage_rule_version,
        )

    def test_v1_preserves_zero_preclose_as_a_discontinuity(self) -> None:
        guarded = self._guarded(PriceLineageGuardV1())
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE stock_daily SET preclose=0 WHERE trade_date=?",
                (self.sessions[1],),
            )

        result = guarded.calculate(
            instrument="000001",
            observation_session=self.sessions[0],
            windows=(1,),
            outcome_as_of=self.sessions[1],
        )

        self.assertEqual(OutcomeStatus.UNAVAILABLE, result.windows[0].status)
        self.assertEqual(
            ("corporate_action_or_price_lineage_discontinuity",),
            result.windows[0].reason_codes,
        )

    def test_v2_uses_pct_change_only_when_reported_preclose_is_invalid(self) -> None:
        guarded = self._guarded(PriceLineageGuardV2())
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE stock_daily SET preclose=0, pct_chg=10 WHERE trade_date=?",
                (self.sessions[1],),
            )

        result = guarded.calculate(
            instrument="000001",
            observation_session=self.sessions[0],
            windows=(1,),
            outcome_as_of=self.sessions[1],
        )

        self.assertEqual(OutcomeStatus.COMPLETE, result.windows[0].status)
        self.assertEqual(("implied_from_pct_chg",), result.price_lineage_sources)
        self.assertEqual(
            "price-lineage-guard.candidate.v2",
            result.price_lineage_rule_version,
        )

    def test_v2_rejects_bad_implied_lineage_and_does_not_override_valid_preclose(self) -> None:
        guard = PriceLineageGuardV2()
        implied_bad = guard.check(
            previous_close=100,
            current_preclose=0,
            current_close=110,
            current_pct_chg=5,
        )
        reported_bad = guard.check(
            previous_close=100,
            current_preclose=50,
            current_close=110,
            current_pct_chg=10,
        )

        self.assertEqual(
            "corporate_action_or_price_lineage_discontinuity",
            implied_bad.reason_code,
        )
        self.assertEqual("implied_from_pct_chg", implied_bad.reference_source)
        self.assertEqual(
            "corporate_action_or_price_lineage_discontinuity",
            reported_bad.reason_code,
        )
        self.assertEqual("reported_preclose", reported_bad.reference_source)

    def test_v2_missing_fallback_inputs_remain_unavailable(self) -> None:
        check = PriceLineageGuardV2().check(
            previous_close=100,
            current_preclose=0,
            current_close=110,
            current_pct_chg=None,
        )

        self.assertEqual("price_lineage_input_missing", check.reason_code)
        self.assertEqual("unavailable", check.reference_source)

    def test_pending_result_does_not_read_lineage_after_cutoff(self) -> None:
        guarded = self._guarded(PriceLineageGuardV2())
        before = guarded.calculate(
            instrument="000001",
            observation_session=self.sessions[3],
            windows=(3,),
            outcome_as_of=self.sessions[4],
        )
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE stock_daily SET preclose=0, pct_chg=NULL WHERE trade_date=?",
                (self.sessions[5],),
            )
        after = guarded.calculate(
            instrument="000001",
            observation_session=self.sessions[3],
            windows=(3,),
            outcome_as_of=self.sessions[4],
        )

        self.assertEqual(before, after)
        self.assertEqual(OutcomeStatus.PENDING, after.windows[0].status)

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

    def test_batch_is_scalar_equivalent_and_reads_each_session_once(self) -> None:
        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                "UPDATE stock_daily SET preclose=0, pct_chg=10 WHERE stock_code=? AND trade_date=?",
                ("000001", self.sessions[1]),
            )
        counting = _CountingReader(MarketDataReaderV1(
            MarketDataSourceV1(self.db_path, "fixture-market.v1")
        ))
        calculator = OutcomeCalculatorV1(
            calendar=TradingCalendarV1(
                self.sessions, source_version="fixture-calendar.v1"
            ),
            market_data=counting,
            price_basis_version="raw-close-ohLC.v1",
            price_lineage_guard=PriceLineageGuardV2(),
        )
        requests = (
            OutcomeRequestV1("000001", self.sessions[0], (1, 3, 5)),
            OutcomeRequestV1("000002", self.sessions[0], (1, 3, 5)),
        )
        scalar = tuple(calculator.calculate(
            instrument=request.instrument,
            observation_session=request.observation_session,
            windows=request.windows,
            outcome_as_of=self.sessions[4],
        ) for request in requests)
        counting.calls.clear()

        batch = calculator.calculate_many(
            requests=requests, outcome_as_of=self.sessions[4]
        )

        self.assertIsInstance(batch, OutcomeBatchResultV1)
        self.assertEqual(OUTCOME_BATCH_VERSION, batch.schema_version)
        self.assertEqual(scalar, batch.results)
        self.assertEqual(self.sessions[:5], tuple(counting.calls))

    def _guarded(self, guard):
        return OutcomeCalculatorV1(
            calendar=TradingCalendarV1(
                self.sessions, source_version="fixture-calendar.v1"
            ),
            market_data=MarketDataReaderV1(
                MarketDataSourceV1(self.db_path, "fixture-market.v1")
            ),
            price_basis_version="raw-close-ohLC.v1",
            price_lineage_guard=guard,
        )

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
            for code, multiplier in (("000001", 1.0), ("000002", 2.0)):
                previous_close = None
                for session, (close, high, low) in zip(self.sessions, prices):
                    rows.append((
                        code, "测试", session,
                        close * multiplier, high * multiplier, low * multiplier,
                        close * multiplier,
                        previous_close if previous_close is not None else close * multiplier,
                        1, 1, 0, 0, 0,
                    ))
                    previous_close = close * multiplier
            connection.executemany("INSERT INTO stock_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
