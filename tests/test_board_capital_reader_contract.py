from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.board_capital import (
    BoardFactReaderV1,
    CapitalFactReaderV1,
    SectorMarketFactReaderV1,
)
from yifei_platform.market_data import ReadStatus


class BoardCapitalReaderContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tempdir.name) / "market.db"
        self._seed(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_board_reader_returns_exact_historical_facts(self) -> None:
        reader = BoardFactReaderV1(self.db_path, source_version="boards.2026-07-10.v1")
        result = reader.read_daily("2026-07-09")
        self.assertEqual(ReadStatus.OK, result.status)
        self.assertEqual("2026-07-10", result.latest_available_as_of)
        self.assertEqual("B001", result.facts[0].board_code)
        self.assertEqual(1.2, result.facts[0].pct_chg)

    def test_capital_reader_preserves_units_and_missing_values(self) -> None:
        reader = CapitalFactReaderV1(self.db_path, source_version="sector-flow.2026-07-10.v1")
        result = reader.read_sector_daily("2026-07-10")
        self.assertTrue(result.ok)
        fact = result.facts[0]
        self.assertEqual(123456789.0, fact.main_inflow)
        self.assertEqual("CNY", fact.amount_unit)
        self.assertEqual("CNY", fact.main_inflow_unit)
        self.assertEqual(18, fact.up_count)
        self.assertIsNone(fact.lead_stock_chg)
        self.assertFalse(hasattr(fact, "score"))
        self.assertFalse(hasattr(fact, "action"))

    def test_missing_date_and_required_schema_are_explicit(self) -> None:
        board = BoardFactReaderV1(self.db_path, source_version="boards.v1")
        self.assertEqual(ReadStatus.MISSING, board.read_daily("2026-07-08").status)
        invalid = Path(self.tempdir.name) / "invalid.db"
        with sqlite3.connect(invalid) as connection:
            connection.execute("CREATE TABLE sector_fund_flow_daily (trade_date TEXT)")
        capital = CapitalFactReaderV1(invalid, source_version="flow.v1")
        result = capital.read_sector_daily("2026-07-10")
        self.assertEqual(ReadStatus.BLOCKED, result.status)
        self.assertIn("required_column_missing:sector_code", result.reason_codes)

    def test_sector_market_reader_isolates_taxonomy_and_source_version(self) -> None:
        reader = SectorMarketFactReaderV1(
            self.db_path, sector_level="THS_L2",
            source_version="platform-stock-daily-ths-l2.v1",
        )
        result = reader.read_daily("2026-07-10")

        self.assertTrue(result.ok)
        self.assertEqual("sector_market_daily_ths_l2", result.dataset)
        self.assertEqual(1, len(result.facts))
        self.assertEqual("机器人", result.facts[0].sector_name)
        self.assertEqual(1.2, result.facts[0].equal_weight_return_pct)
        self.assertEqual(1234.0, result.facts[0].amount)

        wrong_source = SectorMarketFactReaderV1(
            self.db_path, sector_level="THS_L2", source_version="wrong.v1",
        ).read_daily("2026-07-10")
        self.assertEqual(ReadStatus.MISSING, wrong_source.status)

    @staticmethod
    def _seed(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.execute(
                """CREATE TABLE ths_board_daily (
                    board_code TEXT, board_name TEXT, trade_date TEXT,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, amount REAL, pct_chg REAL,
                    PRIMARY KEY(board_code, trade_date))"""
            )
            connection.executemany(
                "INSERT INTO ths_board_daily VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    ("B001", "机器人", "2026-07-09", 100, 103, 99, 102, 1, 2, 1.2),
                    ("B001", "机器人", "2026-07-10", 102, 104, 101, 103, 1, 2, 0.9),
                ],
            )
            connection.execute(
                """CREATE TABLE sector_fund_flow_daily (
                    trade_date TEXT, sector_code TEXT, sector_name TEXT,
                    amount REAL, change_pct REAL, main_inflow REAL,
                    up_count INTEGER, down_count INTEGER,
                    lead_stock_name TEXT, lead_stock_chg REAL,
                    amount_unit TEXT NOT NULL,
                    main_inflow_unit TEXT NOT NULL,
                    PRIMARY KEY(trade_date, sector_code))"""
            )
            connection.execute(
                """INSERT INTO sector_fund_flow_daily
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "2026-07-10", "S001", "机器人", 987654321,
                    2.1, 123456789, 18, 3, "测试股份", None,
                    "CNY", "CNY",
                ),
            )
            connection.execute(
                """CREATE TABLE sector_market_daily (
                    sector_code TEXT, sector_name TEXT, sector_level TEXT,
                    trade_date TEXT, member_count INTEGER,
                    observed_member_count INTEGER,
                    equal_weight_return_pct REAL, amount REAL,
                    amount_unit TEXT, coverage REAL, source TEXT,
                    source_version TEXT, membership_source_version TEXT,
                    published_at TEXT)"""
            )
            connection.executemany(
                "INSERT INTO sector_market_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (
                        "THS:001", "机器人", "THS_L2", "2026-07-10",
                        10, 10, 1.2, 1234.0, "CNY", 1.0, "platform",
                        "platform-stock-daily-ths-l2.v1", "membership.v1",
                        "2026-07-10T10:00:00+00:00",
                    ),
                    (
                        "SW:001", "自动化设备", "L2", "2026-07-10",
                        12, 12, 2.3, 2345.0, "CNY", 1.0, "platform",
                        "platform-stock-daily-cninfo-sw-l2.v2",
                        "membership.v2", "2026-07-10T10:00:00+00:00",
                    ),
                ),
            )
