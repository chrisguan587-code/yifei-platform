from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.bootstrap import bootstrap_market_data, load_trading_sessions
from yifei_platform.readiness import ReadinessStoreV1


class BootstrapMarketDataTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "legacy.db"
        self.target = self.root / "shared" / "market_data.db"
        self.readiness = self.root / "readiness"
        with sqlite3.connect(self.source) as connection:
            connection.executescript("""
                CREATE TABLE stock_daily (
                    stock_code TEXT, stock_name TEXT, trade_date TEXT, open REAL,
                    high REAL, low REAL, close REAL, preclose REAL, volume REAL,
                    amount REAL, pct_chg REAL, turnover REAL, is_st INTEGER,
                    PRIMARY KEY (stock_code, trade_date)
                );
                CREATE TABLE v3_private_score (stock_code TEXT, score REAL);
                INSERT INTO stock_daily VALUES
                    ('000001','A','2026-07-09',1,1,1,1,1,10,100,0,1,0),
                    ('000001','A','2026-07-10',1,1,1,1,1,10,200,0,1,0);
                INSERT INTO v3_private_score VALUES ('000001', 99);
            """)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_publishes_only_public_facts_calendar_and_ready_marker(self) -> None:
        result = self._publish()
        self.assertEqual("2026-07-10", result.as_of)
        self.assertEqual(2, result.row_count)
        self.assertEqual(("2026-07-09", "2026-07-10"), load_trading_sessions(self.target))
        with sqlite3.connect(self.target) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertNotIn("v3_private_score", tables)
        marker = ReadinessStoreV1(self.readiness).read_ready(
            bundle="v4-market-core", as_of="2026-07-10"
        )
        self.assertEqual(("stock_daily",), marker.required_datasets)

    def test_output_survives_source_removal_and_repeat_is_idempotent(self) -> None:
        first = self._publish()
        second = self._publish()
        self.assertEqual(first.database_sha256, second.database_sha256)
        self.source.rename(self.source.with_suffix(".retired"))
        self.assertEqual(("2026-07-09", "2026-07-10"), load_trading_sessions(self.target))

    def test_different_existing_target_is_not_overwritten(self) -> None:
        self.target.parent.mkdir(parents=True)
        self.target.write_bytes(b"different")
        with self.assertRaises(FileExistsError):
            self._publish()
        self.assertEqual(b"different", self.target.read_bytes())

    def test_invalid_timestamp_leaves_no_database_or_readiness(self) -> None:
        with self.assertRaises(ValueError):
            bootstrap_market_data(
                source_path=self.source,
                target_path=self.target,
                readiness_root=self.readiness,
                published_at="2026-07-12T10:00:00",
            )
        self.assertFalse(self.target.exists())
        self.assertFalse(self.readiness.exists())

    def _publish(self):
        return bootstrap_market_data(
            source_path=self.source,
            target_path=self.target,
            readiness_root=self.readiness,
            published_at="2026-07-12T10:00:00+08:00",
        )


if __name__ == "__main__":
    unittest.main()
