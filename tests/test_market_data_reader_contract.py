from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.market_data import MarketDataReaderV1, MarketDataSourceV1, ReadStatus


FIXTURE = Path(__file__).parent / "fixtures" / "market_contract_v1.json"


class MarketDataReaderContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "market_data.db"
        self._seed_database(self.db_path, self.payload["stock_daily"])
        self.reader = MarketDataReaderV1(
            MarketDataSourceV1(
                database_path=self.db_path,
                source_version=self.payload["market_source_version"],
            )
        )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_reads_all_market_facts_without_v3_universe_filter(self) -> None:
        result = self.reader.read_stock_daily("2026-06-05")
        self.assertEqual(ReadStatus.OK, result.status)
        self.assertEqual(["000001", "688001"], [fact.stock_code for fact in result.facts])
        self.assertEqual("2026-06-05", result.latest_available_as_of)
        self.assertEqual("fixture-market.2026-06-05.v1", result.source_version)
        self.assertEqual("market-data.stock-daily.v1", result.schema_version)

    def test_historical_as_of_remains_valid_when_newer_data_exists(self) -> None:
        result = self.reader.read_stock_daily("2026-06-03")
        self.assertEqual(ReadStatus.OK, result.status)
        self.assertEqual("600001", result.facts[0].stock_code)
        self.assertEqual("2026-06-05", result.latest_available_as_of)

    def test_missing_as_of_is_explicit(self) -> None:
        result = self.reader.read_stock_daily("2026-06-04")
        self.assertEqual(ReadStatus.MISSING, result.status)
        self.assertEqual(("stock_daily_as_of_missing",), result.reason_codes)
        self.assertEqual((), result.facts)

    def test_optional_columns_are_missing_not_invented(self) -> None:
        minimal_db = Path(self.tempdir.name) / "minimal.db"
        with sqlite3.connect(minimal_db) as connection:
            connection.execute("CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)")
            connection.execute("INSERT INTO stock_daily VALUES ('600001', '2026-06-05')")
        reader = MarketDataReaderV1(MarketDataSourceV1(minimal_db, "minimal.v1"))
        result = reader.read_stock_daily("2026-06-05")
        self.assertTrue(result.ok)
        self.assertIsNone(result.facts[0].close)
        self.assertIsNone(result.facts[0].is_st)

    def test_missing_required_column_blocks_read(self) -> None:
        invalid_db = Path(self.tempdir.name) / "invalid.db"
        with sqlite3.connect(invalid_db) as connection:
            connection.execute("CREATE TABLE stock_daily (trade_date TEXT)")
        reader = MarketDataReaderV1(MarketDataSourceV1(invalid_db, "invalid.v1"))
        result = reader.read_stock_daily("2026-06-05")
        self.assertEqual(ReadStatus.BLOCKED, result.status)
        self.assertEqual(("required_column_missing:stock_code",), result.reason_codes)

    def test_missing_database_does_not_get_created(self) -> None:
        missing = Path(self.tempdir.name) / "missing.db"
        reader = MarketDataReaderV1(MarketDataSourceV1(missing, "missing.v1"))
        result = reader.read_stock_daily("2026-06-05")
        self.assertEqual(ReadStatus.MISSING, result.status)
        self.assertFalse(missing.exists())

    def test_invalid_as_of_and_source_version_fail_fast(self) -> None:
        with self.assertRaises(ValueError):
            self.reader.read_stock_daily("2026-02-30")
        with self.assertRaises(ValueError):
            MarketDataSourceV1(self.db_path, "")

    @staticmethod
    def _seed_database(path: Path, rows: list[dict]) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                """
                CREATE TABLE stock_daily (
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    trade_date TEXT NOT NULL,
                    open REAL,
                    high REAL,
                    low REAL,
                    close REAL,
                    preclose REAL,
                    volume REAL,
                    amount REAL,
                    pct_chg REAL,
                    turnover REAL,
                    is_st INTEGER,
                    PRIMARY KEY (stock_code, trade_date)
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO stock_daily VALUES (
                    :stock_code, :stock_name, :trade_date, :open, :high, :low,
                    :close, :preclose, :volume, :amount, :pct_chg, :turnover, :is_st
                )
                """,
                rows,
            )
