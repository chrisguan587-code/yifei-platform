from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.backtest import (
    BacktestEngineV1,
    BacktestRunRequestV1,
    BacktestStatus,
    ExecutionPolicyV1,
    FeeRuleV1,
    FeeScheduleV1,
    FrozenOrderIntentProviderV1,
    OrderIntentV1,
)
from yifei_platform.backtest_cli import run_from_files
from yifei_platform.calendar import TradingCalendarV1
from yifei_platform.market_data import MarketDataReaderV1, MarketDataSourceV1


class BacktestEngineContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.database = self.root / "market.db"
        self.sessions = tuple(
            (date(2026, 1, 1) + timedelta(days=offset)).isoformat()
            for offset in range(12)
        )
        self._seed_database()
        self.calendar = TradingCalendarV1(
            self.sessions, source_version="fixture-calendar.v1"
        )
        self.fees = FeeScheduleV1(
            version="fixture-fees.v1",
            rules=(FeeRuleV1(
                effective_from=self.sessions[0],
                commission_rate=0.0003,
                minimum_commission=5.0,
                stamp_duty_sell_rate=0.0005,
                transfer_fee_rate=0.00001,
            ),),
        )
        self.policy = ExecutionPolicyV1(
            version="fixture-execution.v1",
            slippage_rate=0.0,
            maximum_volume_participation=0.10,
            volume_lookback_sessions=3,
            minimum_volume_observations=3,
        )

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_next_open_t1_round_lot_fees_and_cash_reconcile(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE stock_daily SET open=11, high=11, low=11, close=11 "
                "WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[7],),
            )
        buy = self._buy("buy-1", decided=5, execute=6, cash_budget=10_006)
        sell = OrderIntentV1(
            intent_id="sell-1", instrument="000001", side="sell",
            decided_as_of=self.sessions[6], earliest_execution_session=self.sessions[7],
            submission_sequence=1,
        )

        result = self._run((buy, sell), market_end=8)

        self.assertEqual(BacktestStatus.COMPLETE, result.status)
        self.assertEqual([self.sessions[6], self.sessions[7]], [row["session"] for row in result.fills])
        self.assertEqual([1000, 1000], [row["quantity"] for row in result.fills])
        self.assertAlmostEqual(5.1, result.fills[0]["fees"], places=4)
        self.assertAlmostEqual(10.61, result.fills[1]["fees"], places=4)
        self.assertEqual(1, result.summary["closed_trade_count"])
        self.assertAlmostEqual(984.29, result.trades[0]["net_profit"], places=4)
        self.assertAlmostEqual(100_984.29, result.summary["final_nav"], places=4)
        self.assertAlmostEqual(
            result.summary["final_nav"] - result.summary["initial_cash"],
            result.summary["realized_profit"] + result.summary["unrealized_profit"],
            places=4,
        )
        final = result.daily_snapshots[-1]
        self.assertAlmostEqual(final["cash"] + final["market_value"], final["nav"], places=4)

    def test_buy_budget_includes_minimum_fee_and_never_creates_odd_lot(self) -> None:
        result = self._run((self._buy("buy-budget", decided=5, execute=6, cash_budget=10_000),))

        self.assertEqual(900, result.fills[0]["quantity"])
        self.assertEqual(0, result.fills[0]["quantity"] % 100)
        self.assertGreaterEqual(result.summary["final_cash"], 0)

    def test_liquidity_capacity_causes_explicit_partial_fill(self) -> None:
        intent = self._buy("buy-large", decided=5, execute=6, quantity=5_000)

        result = self._run((intent,))

        self.assertEqual(1000, result.fills[0]["quantity"])
        self.assertEqual("partially_filled", result.fills[0]["status"])
        self.assertEqual("partially_filled", result.orders[0]["status"])
        self.assertEqual(5000, result.orders[0]["requested_quantity"])
        self.assertEqual(4000, result.orders[0]["unfilled_quantity"])
        self.assertEqual(0, result.daily_snapshots[-1]["pending_order_count"])

    def test_partial_sell_retries_only_the_unfilled_requested_quantity(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE stock_daily SET volume=2000 WHERE stock_code='000001' "
                "AND trade_date BETWEEN ? AND ?",
                (self.sessions[5], self.sessions[10]),
            )
        buy = self._buy("buy-before-partial-sell", decided=5, execute=6, quantity=1000)
        sell = OrderIntentV1(
            intent_id="sell-partial", instrument="000001", side="sell",
            decided_as_of=self.sessions[7], earliest_execution_session=self.sessions[8],
            submission_sequence=1, quantity=500,
        )

        result = self._run((buy, sell), market_end=10, decision_end=7)

        sell_fills = [row for row in result.fills if row["side"] == "sell"]
        self.assertEqual(500, sum(row["quantity"] for row in sell_fills))
        self.assertEqual(500, result.positions[0]["quantity"])
        self.assertEqual([300, 100, 0], [
            row["unfilled_quantity"] for row in result.orders if row["side"] == "sell"
        ])

    def test_upper_limit_buy_is_unfilled_and_not_reported_as_zero_signal(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE stock_daily SET open=11, high=11, low=11, close=11 "
                "WHERE stock_code='000002' AND trade_date=?",
                (self.sessions[6],),
            )
        intent = replace(self._buy("buy-limit", decided=5, execute=6), instrument="000002")

        result = self._run((intent,))

        self.assertEqual(1, result.summary["intent_count"])
        self.assertEqual(0, result.summary["fill_count"])
        self.assertEqual("open_at_upper_limit", result.orders[0]["reason"])

    def test_lower_limit_sell_is_carried_and_never_force_closed(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE stock_daily SET open=9, high=9, low=9, close=9 "
                "WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[7],),
            )
        buy = self._buy("buy-before-down", decided=5, execute=6)
        sell = OrderIntentV1(
            intent_id="sell-at-down", instrument="000001", side="sell",
            decided_as_of=self.sessions[6], earliest_execution_session=self.sessions[7],
            submission_sequence=1,
        )

        result = self._run((buy, sell), market_end=7)

        self.assertEqual(BacktestStatus.DEGRADED, result.status)
        self.assertEqual("open_at_lower_limit", result.orders[-1]["reason"])
        self.assertEqual(1, result.summary["open_position_count"])
        self.assertEqual(0, result.summary["closed_trade_count"])
        self.assertIn("orders_pending_at_end", result.reason_codes)

    def test_lower_limit_sell_executes_after_limit_lifts(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE stock_daily SET open=9, high=9, low=9, close=9 "
                "WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[7],),
            )
            connection.execute(
                "UPDATE stock_daily SET open=8.1, high=8.1, low=8.1, close=8.1, preclose=9 "
                "WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[8],),
            )
            connection.execute(
                "UPDATE stock_daily SET open=8.5, high=8.5, low=8.5, close=8.5, preclose=8.1 "
                "WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[9],),
            )
        buy = self._buy("buy-before-two-down-days", decided=5, execute=6)
        sell = OrderIntentV1(
            intent_id="sell-after-limit-lifts", instrument="000001", side="sell",
            decided_as_of=self.sessions[6], earliest_execution_session=self.sessions[7],
            submission_sequence=1,
        )

        result = self._run((buy, sell), market_end=9)

        self.assertEqual(["open_at_lower_limit", "open_at_lower_limit", None], [
            row["reason"] for row in result.orders if row["side"] == "sell"
        ])
        self.assertEqual(self.sessions[9], result.trades[0]["exit_session"])

    def test_last_calendar_session_buy_does_not_leak_cash(self) -> None:
        buy = self._buy("buy-on-last-session", decided=10, execute=11)

        result = self._run((buy,), market_end=11, decision_start=10, decision_end=10)

        self.assertEqual(100000, result.summary["final_cash"])
        self.assertEqual(0, result.summary["fill_count"])
        self.assertEqual("settlement_session_unavailable", result.orders[0]["reason"])

    def test_beijing_exchange_43_prefix_uses_thirty_percent_limit(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE stock_daily SET open=13, high=13, low=13, close=13 "
                "WHERE stock_code='430001' AND trade_date=?",
                (self.sessions[6],),
            )
        intent = replace(
            self._buy("buy-bse-limit", decided=5, execute=6), instrument="430001"
        )

        result = self._run((intent,))

        self.assertEqual("open_at_upper_limit", result.orders[0]["reason"])

    def test_end_of_range_marks_open_position_without_illegal_liquidation(self) -> None:
        result = self._run((self._buy("buy-open", decided=5, execute=6),), market_end=6)

        self.assertEqual(1, result.summary["open_position_count"])
        self.assertEqual(0, result.summary["closed_trade_count"])
        self.assertEqual(0, result.positions[0]["sellable_quantity"])
        self.assertIn("unsettled_positions_at_end", result.reason_codes)

    def test_position_price_discontinuity_blocks_instead_of_inventing_profit(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "UPDATE stock_daily SET preclose=9 WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[7],),
            )

        with self.assertRaisesRegex(ValueError, "price-lineage discontinuity"):
            self._run((self._buy("buy-before-discontinuity", decided=5, execute=6),), market_end=7)

    def test_provider_future_read_and_provider_failure_are_not_silently_empty(self) -> None:
        outer = self

        class FutureReader:
            def on_session_close(self, context):
                context.market.fact("000001", outer.sessions[7])
                return ()

        with self.assertRaisesRegex(RuntimeError, "provider failed") as caught:
            self._engine().run(request=self._request(market_end=8), provider=FutureReader())
        self.assertIn("point-in-time", str(caught.exception.__cause__))

    def test_same_frozen_inputs_are_deterministic(self) -> None:
        intents = (
            self._buy("buy-deterministic", decided=5, execute=6),
            OrderIntentV1(
                intent_id="sell-deterministic", instrument="000001", side="sell",
                decided_as_of=self.sessions[6], earliest_execution_session=self.sessions[7],
                submission_sequence=1,
            ),
        )

        first = self._run(intents, market_end=8)
        second = self._run(intents, market_end=8)

        self.assertEqual(first, second)

    def test_unsupported_execution_models_and_adjusted_prices_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "unsupported liquidity"):
            replace(self.policy, liquidity_model_version="unknown.v2")
        with self.assertRaisesRegex(ValueError, "raw-unadjusted"):
            replace(self._request(), price_basis_version="adjusted.v1").validate(self.calendar)

    def test_manual_cli_freezes_database_and_refuses_run_id_reuse(self) -> None:
        spec = self.root / "spec.json"
        spec.write_text(json.dumps({
            "run_id": "fixture-run-001",
            "decision_start_session": self.sessions[5],
            "decision_end_session": self.sessions[6],
            "market_data_start_session": self.sessions[0],
            "market_data_end_session": self.sessions[8],
            "initial_cash": 100000,
            "maximum_positions": 2,
            "market_data_source_version": "fixture-market.v1",
            "calendar_version": "fixture-calendar.v1",
            "price_basis_version": "raw-unadjusted.v1",
            "caller": "manual-test",
            "caller_version": "v1",
            "adapter_version": "frozen-intents.v1",
            "fee_schedule": {
                "version": self.fees.version,
                "rules": [{
                    "effective_from": self.sessions[0],
                    "commission_rate": 0.0003,
                    "minimum_commission": 5.0,
                    "stamp_duty_sell_rate": 0.0005,
                    "transfer_fee_rate": 0.00001,
                }],
            },
            "execution_policy": {
                "version": self.policy.version,
                "slippage_rate": 0,
                "maximum_volume_participation": 0.1,
                "volume_lookback_sessions": 3,
                "minimum_volume_observations": 3,
            },
            "intents": [{
                "intent_id": "cli-buy", "instrument": "000001", "side": "buy",
                "decided_as_of": self.sessions[5],
                "earliest_execution_session": self.sessions[6],
                "submission_sequence": 1, "cash_budget": 10006,
            }],
        }, ensure_ascii=False), encoding="utf-8")
        output_root = self.root / "runs"

        output = run_from_files(spec_path=spec, market_db=self.database, output_root=output_root)

        expected = {
            "manifest.json", "result.json", "intents.json", "orders.json", "fills.json",
            "trades.json", "positions.json", "daily_snapshots.json", "summary.json",
            "report.md", "market_data.snapshot.db",
        }
        self.assertEqual(expected, {path.name for path in output.iterdir()})
        manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(
            manifest["request"]["market_data_snapshot_sha256"],
            manifest["snapshot_sha256"],
        )
        self.assertEqual(0o444, (output / "market_data.snapshot.db").stat().st_mode & 0o777)
        with self.assertRaises(FileExistsError):
            run_from_files(spec_path=spec, market_db=self.database, output_root=output_root)

    def test_missing_market_bar_degrades_instead_of_looking_like_normal_no_fill(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.execute(
                "DELETE FROM stock_daily WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[6],),
            )

        result = self._run((self._buy("missing-bar", decided=5, execute=6),))

        self.assertEqual(BacktestStatus.DEGRADED, result.status)
        self.assertIn("market_bar_missing", result.reason_codes)
        self.assertEqual("market_bar_missing", result.orders[0]["reason"])

    def _run(self, intents, *, market_end=8, decision_start=5, decision_end=6):
        return self._engine().run(
            request=self._request(
                market_end=market_end,
                decision_start=decision_start,
                decision_end=decision_end,
            ),
            provider=FrozenOrderIntentProviderV1(tuple(intents)),
        )

    def _engine(self):
        return BacktestEngineV1(
            calendar=self.calendar,
            market_data=MarketDataReaderV1(MarketDataSourceV1(
                self.database, source_version="fixture-market.v1"
            )),
            fee_schedule=self.fees,
            execution_policy=self.policy,
        )

    def _request(self, *, market_end=8, decision_start=5, decision_end=6):
        return BacktestRunRequestV1(
            run_id="fixture-run",
            decision_start_session=self.sessions[decision_start],
            decision_end_session=self.sessions[decision_end],
            market_data_start_session=self.sessions[0],
            market_data_end_session=self.sessions[market_end],
            initial_cash=100000,
            maximum_positions=2,
            market_data_snapshot_ref=str(self.database),
            market_data_snapshot_sha256="fixture-sha",
            market_data_source_version="fixture-market.v1",
            calendar_version="fixture-calendar.v1",
            price_basis_version="raw-unadjusted.v1",
            caller="fixture",
            caller_version="v1",
            adapter_version="fixture-adapter.v1",
            platform_version="fixture-platform.v1",
        )

    def _buy(self, identity, *, decided, execute, cash_budget=None, quantity=None):
        if cash_budget is None and quantity is None:
            quantity = 1000
        return OrderIntentV1(
            intent_id=identity,
            instrument="000001",
            side="buy",
            decided_as_of=self.sessions[decided],
            earliest_execution_session=self.sessions[execute],
            submission_sequence=1,
            cash_budget=cash_budget,
            quantity=quantity,
        )

    def _seed_database(self) -> None:
        with sqlite3.connect(self.database) as connection:
            connection.executescript("""
                CREATE TABLE stock_daily (
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    trade_date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL, preclose REAL,
                    volume REAL, amount REAL, pct_chg REAL, turnover REAL, is_st INTEGER
                );
                CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
                CREATE TABLE platform_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)
            connection.executemany(
                "INSERT INTO trading_calendar VALUES (?)", ((session,) for session in self.sessions)
            )
            connection.executemany("INSERT INTO platform_metadata VALUES (?, ?)", (
                ("schema_version", "fixture.v1"),
                ("producer_version", "fixture.v1"),
                ("published_at", "2026-01-12T18:00:00+08:00"),
            ))
            for code in ("000001", "000002", "430001"):
                for index, session in enumerate(self.sessions):
                    open_price = 10.0
                    connection.execute(
                        "INSERT INTO stock_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            code, f"股票{code}", session,
                            open_price, max(open_price, 10.5), min(open_price, 9.5), open_price,
                            10.0, 10_000.0, 100_000.0, (open_price / 10 - 1) * 100, 1.0, 0,
                        ),
                    )


if __name__ == "__main__":
    unittest.main()
