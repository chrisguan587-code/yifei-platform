from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.daily_market import (
    DailyMarketQualityPolicyV1,
    INDEX_MISSING_CORRECTION_VERSION,
    TencentCsi300SnapshotClientV1,
    correct_missing_csi300_index_v1,
    publish_platform_daily_market_data,
    repair_recent_missing_csi300_v1,
)
from unittest.mock import patch
from yifei_platform.bootstrap import load_market_metadata, load_trading_sessions
from yifei_platform.market_observation import (
    append_missing_index_fact_v1,
    append_market_observation_facts_v1,
    initialize_market_observation_schema_v1,
)
from yifei_platform.readiness import ReadinessStoreV1


class StubSnapshotClient:
    source_version = "stub-sina.v1"
    universe_discovery_complete = True

    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.calls = 0

    def fetch(self, *, as_of: str, prior_stock_codes) -> list[dict[str, object]]:
        del as_of, prior_stock_codes
        self.calls += 1
        return self.rows


class StubIndexClient:
    source_version = "stub-csi300.v1"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, *, as_of: str) -> dict[str, object]:
        self.calls += 1
        return {
            "date": as_of,
            "open": 4000.0,
            "high": 4050.0,
            "low": 3980.0,
            "close": 4030.0,
            "volume": 1000.0,
            "amount": 2000.0,
        }


class PlatformDailyMarketPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.target = self.root / "shared" / "market_data.db"
        self.readiness = self.root / "state"
        self.target.parent.mkdir(parents=True)
        with sqlite3.connect(self.target) as connection:
            connection.executescript("""
                CREATE TABLE stock_daily (
                    stock_code TEXT NOT NULL, stock_name TEXT, trade_date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL, preclose REAL,
                    volume REAL, amount REAL, pct_chg REAL, turnover REAL, is_st INTEGER,
                    PRIMARY KEY (stock_code, trade_date)
                );
                CREATE INDEX idx_stock_daily_trade_date ON stock_daily(trade_date);
                CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
                CREATE TABLE platform_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO stock_daily VALUES
                    ('000001','A','2026-08-24',10,11,9,10,10,100,1000,0,NULL,0),
                    ('600000','B','2026-08-24',20,21,19,20,20,200,4000,0,NULL,0);
                INSERT INTO trading_calendar VALUES ('2026-08-24');
                INSERT INTO platform_metadata VALUES
                    ('schema_version','market-data.bootstrap.v1'),
                    ('producer_version','bootstrap-market-data.v1'),
                    ('published_at','2026-08-24T17:00:00+08:00');
            """)
            initialize_market_observation_schema_v1(connection)
            append_market_observation_facts_v1(
                connection,
                as_of="2026-08-24",
                index_row=None,
                index_source_version=None,
            )
        self.policy = DailyMarketQualityPolicyV1(
            minimum_rows=2, minimum_prior_code_coverage=1.0
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_appends_exact_date_and_publishes_readiness_without_v3(self) -> None:
        result = self._publish(self._rows(), index_client=StubIndexClient())

        self.assertEqual("2026-08-25", result.as_of)
        self.assertEqual(4, result.row_count)
        self.assertEqual(
            ("2026-08-24", "2026-08-25"), load_trading_sessions(self.target)
        )
        metadata = load_market_metadata(self.target)
        self.assertEqual("platform-daily-market.v2", metadata["producer_version"])
        self.assertNotIn("yifei_V3", str(metadata))
        with sqlite3.connect(self.target) as connection:
            rows = connection.execute(
                "SELECT stock_code,turnover,is_st FROM stock_daily "
                "WHERE trade_date='2026-08-25' ORDER BY stock_code"
            ).fetchall()
            breadth = connection.execute(
                "SELECT advance_count,decline_count,valid_return_count "
                "FROM market_breadth_daily WHERE trade_date='2026-08-25'"
            ).fetchone()
            index_row = connection.execute(
                "SELECT index_code,close,source_version FROM index_daily "
                "WHERE trade_date='2026-08-25'"
            ).fetchone()
        self.assertEqual([("000001", None, 0), ("600000", None, 1)], rows)
        self.assertEqual((1, 1, 2), breadth)
        self.assertEqual(("000300.SH", 4030.0, "stub-csi300.v1"), index_row)
        marker = ReadinessStoreV1(self.readiness).read_ready(
            bundle="v4-market-core", as_of="2026-08-25"
        )
        self.assertIsNotNone(marker)
        self.assertEqual("platform-daily-market.v2", marker.producer_version)

    def test_same_day_retry_is_idempotent_but_changed_content_is_rejected(self) -> None:
        first = self._publish(self._rows())
        retry_index = StubIndexClient()
        second = self._publish(
            self._rows(), index_client=retry_index,
            published_at="2026-08-25T19:00:00+08:00",
        )
        self.assertEqual(first, second)
        self.assertEqual(0, retry_index.calls)

        changed = self._rows()
        changed[0] = {**changed[0], "成交额": 9999.0}
        original = self.target.read_bytes()
        with self.assertRaisesRegex(ValueError, "explicit correction"):
            self._publish(changed, published_at="2026-08-25T20:00:00+08:00")
        self.assertEqual(original, self.target.read_bytes())

    def test_missing_only_index_correction_preserves_frozen_market_facts(self) -> None:
        self._publish(self._rows())
        with sqlite3.connect(self.target) as connection:
            protected_before = {
                table: connection.execute(f"SELECT * FROM {table}").fetchall()
                for table in (
                    "stock_daily", "market_breadth_daily",
                    "trading_calendar", "platform_metadata",
                )
            }

        corrected = correct_missing_csi300_index_v1(
            target_path=self.target,
            as_of="2026-08-25",
            corrected_at="2026-08-25T19:00:00+08:00",
            index_client=StubIndexClient(),
        )

        self.assertTrue(corrected)
        with sqlite3.connect(self.target) as connection:
            for table, expected in protected_before.items():
                self.assertEqual(
                    expected, connection.execute(f"SELECT * FROM {table}").fetchall()
                )
            index = connection.execute(
                "SELECT close,source_version FROM index_daily WHERE trade_date=?",
                ("2026-08-25",),
            ).fetchone()
            audit = connection.execute(
                "SELECT contract_version,fact_key FROM platform_fact_corrections"
            ).fetchone()
        self.assertEqual((4030.0, "stub-csi300.v1"), index)
        self.assertEqual(
            (INDEX_MISSING_CORRECTION_VERSION, "000300.SH:2026-08-25"), audit
        )
        self.assertFalse(correct_missing_csi300_index_v1(
            target_path=self.target,
            as_of="2026-08-25",
            corrected_at="2026-08-25T20:00:00+08:00",
            index_client=StubIndexClient(),
        ))

    def test_recent_repair_only_attempts_missing_sessions(self) -> None:
        self._publish(self._rows())
        result = repair_recent_missing_csi300_v1(
            target_path=self.target,
            corrected_at="2026-08-25T19:00:00+08:00",
            client_factory=StubIndexClient,
            lookback_sessions=2,
        )
        self.assertEqual(("2026-08-24", "2026-08-25"), result["corrected"])
        self.assertEqual((), result["failed"])

    def test_tencent_index_quote_units_and_date_are_validated(self) -> None:
        fields = [""] * 70
        fields[2] = "000300"
        fields[3] = "4030"
        fields[4] = "4000"
        fields[5] = "4010"
        fields[6] = "1000"
        fields[30] = "20260825161413"
        fields[33] = "4050"
        fields[34] = "3980"
        fields[37] = "2000"
        payload = f'v_sh000300="{"~".join(fields)}";'.encode("gbk")

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return payload

        with patch("urllib.request.urlopen", return_value=Response()):
            row = TencentCsi300SnapshotClientV1().fetch(as_of="2026-08-25")
        self.assertEqual(100_000.0, row["volume"])
        self.assertEqual(20_000_000.0, row["amount"])
        with patch("urllib.request.urlopen", return_value=Response()):
            with self.assertRaisesRegex(RuntimeError, "row missing"):
                TencentCsi300SnapshotClientV1().fetch(as_of="2026-08-26")

    def test_missing_index_primitive_requires_caller_transaction(self) -> None:
        with sqlite3.connect(self.target) as connection:
            with self.assertRaisesRegex(ValueError, "active transaction"):
                append_missing_index_fact_v1(
                    connection,
                    as_of="2026-08-24",
                    index_row=StubIndexClient().fetch(as_of="2026-08-24"),
                    index_source_version="stub-csi300.v1",
                )

    def test_partial_duplicate_invalid_or_older_snapshot_never_changes_target(self) -> None:
        cases = (
            (self._rows()[:1], "coverage|row count"),
            (self._rows() + [self._rows()[0]], "duplicate"),
            ([{**self._rows()[0], "最新价": 0}, self._rows()[1]], "close"),
        )
        for rows, message in cases:
            with self.subTest(message=message):
                original = self.target.read_bytes()
                with self.assertRaisesRegex(ValueError, message):
                    self._publish(rows)
                self.assertEqual(original, self.target.read_bytes())
                self.assertIsNone(ReadinessStoreV1(self.readiness).read_ready(
                    bundle="v4-market-core", as_of="2026-08-25"
                ))
        with self.assertRaisesRegex(ValueError, "older"):
            publish_platform_daily_market_data(
                client=StubSnapshotClient(self._rows()),
                target_path=self.target,
                readiness_root=self.readiness,
                as_of="2026-08-21",
                published_at="2026-08-25T18:00:00+08:00",
                quality_policy=self.policy,
            )

    def test_explicit_suspended_quote_is_preserved_as_non_trading_fact(self) -> None:
        rows = self._rows()
        rows[0] = {
            **rows[0], "最新价": 0.0, "今开": 0.0, "最高": 0.0,
            "最低": 0.0, "成交量": 0.0, "成交额": 0.0,
        }
        self._publish(rows)
        with sqlite3.connect(self.target) as connection:
            stored = connection.execute(
                "SELECT close,open,high,low,preclose,volume,amount "
                "FROM stock_daily WHERE stock_code='000001' AND trade_date='2026-08-25'"
            ).fetchone()
        self.assertEqual((0.0, None, None, None, 10.0, 0.0, 0.0), stored)

    def test_future_as_of_is_rejected_before_fetch(self) -> None:
        client = StubSnapshotClient(self._rows())
        with self.assertRaisesRegex(ValueError, "after published_at"):
            publish_platform_daily_market_data(
                client=client,
                target_path=self.target,
                readiness_root=self.readiness,
                as_of="2026-08-25",
                published_at="2026-08-24T18:00:00+08:00",
                quality_policy=self.policy,
            )
        self.assertEqual(0, client.calls)

    def test_daily_publication_requires_completed_observation_migration(self) -> None:
        with sqlite3.connect(self.target) as connection:
            connection.execute("DROP TABLE market_breadth_daily")
        client = StubSnapshotClient(self._rows())

        with self.assertRaisesRegex(ValueError, "must be migrated"):
            publish_platform_daily_market_data(
                client=client,
                target_path=self.target,
                readiness_root=self.readiness,
                as_of="2026-08-25",
                published_at="2026-08-25T18:00:00+08:00",
                quality_policy=self.policy,
            )

        self.assertEqual(0, client.calls)

    def _publish(
        self, rows: list[dict[str, object]], *, index_client=None,
        published_at: str = "2026-08-25T18:00:00+08:00",
    ):
        return publish_platform_daily_market_data(
            client=StubSnapshotClient(rows),
            target_path=self.target,
            readiness_root=self.readiness,
            as_of="2026-08-25",
            published_at=published_at,
            index_client=index_client,
            quality_policy=self.policy,
        )

    @staticmethod
    def _rows() -> list[dict[str, object]]:
        return [
            {
                "代码": "sz000001", "名称": "A", "最新价": 10.5,
                "今开": 10.0, "最高": 11.0, "最低": 9.8, "昨收": 10.0,
                "成交量": 120.0, "成交额": 1260.0, "涨跌幅": 5.0,
            },
            {
                "代码": "sh600000", "名称": "*ST B", "最新价": 19.5,
                "今开": 20.0, "最高": 20.2, "最低": 19.0, "昨收": 20.0,
                "成交量": 210.0, "成交额": 4095.0, "涨跌幅": -2.5,
            },
        ]


if __name__ == "__main__":
    unittest.main()
