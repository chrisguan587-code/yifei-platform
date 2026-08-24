from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.board_daily_ingestion import sync_board_daily_v1
from yifei_platform.supplemental_facts import initialize_supplemental_database_v1


class _Client:
    def __init__(self, count: int = 80):
        self.calls: list[tuple[str, str, str]] = []
        self._boards = tuple(
            {"board_code": f"B{index:03d}", "board_name": f"行业{index}"}
            for index in range(count)
        )

    def list_boards(self):
        return self._boards

    def read_history(self, board_name: str, start_date: str, end_date: str):
        self.calls.append((board_name, start_date, end_date))
        rows = (
            {"日期": "2026-08-20", "开盘价": 99, "最高价": 101,
             "最低价": 98, "收盘价": 100, "成交量": 1, "成交额": 10},
            {"日期": "2026-08-21", "开盘价": 100, "最高价": 102,
             "最低价": 99, "收盘价": 101, "成交量": 2, "成交额": 20},
        )
        return tuple(
            row for row in rows
            if start_date <= str(row["日期"]).replace("-", "") <= end_date
        )


class BoardDailyIngestionTests(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.TemporaryDirectory()
        root = Path(self.root.name)
        self.market = root / "market.db"
        self.target = root / "supplemental.db"
        with sqlite3.connect(self.market) as connection:
            connection.executescript(
                """CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
                   CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT);"""
            )
            connection.executemany(
                "INSERT INTO trading_calendar VALUES (?)",
                (("2026-08-20",), ("2026-08-21",)),
            )
            connection.execute(
                "INSERT INTO stock_daily VALUES ('000001', '2026-08-21')"
            )
        initialize_supplemental_database_v1(self.target)
        with sqlite3.connect(self.target) as connection:
            connection.executemany(
                """INSERT INTO ths_board_daily VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [
                    (f"B{index:03d}", f"行业{index}", "2026-08-20", 99, 101,
                     98, 100, 1, 10, 0)
                    for index in range(80)
                ],
            )

    def tearDown(self):
        self.root.cleanup()

    def test_syncs_missing_session_atomically_and_is_idempotent(self):
        client = _Client()
        result = sync_board_daily_v1(
            client=client, market_database_path=self.market,
            target_path=self.target, as_of="2026-08-21",
            fetched_at="2026-08-21T10:30:00+00:00",
        )
        self.assertEqual("2026-08-21", result.first_synced_date)
        self.assertEqual(1, result.synced_session_count)
        self.assertEqual(80, result.inserted_row_count)
        with sqlite3.connect(self.target) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM ths_board_daily WHERE trade_date='2026-08-21'"
            ).fetchone()[0]
            pct_chg = connection.execute(
                "SELECT pct_chg FROM ths_board_daily WHERE board_code='B000' AND trade_date='2026-08-21'"
            ).fetchone()[0]
        self.assertEqual(80, count)
        self.assertAlmostEqual(1.0, pct_chg)
        self.assertEqual(80, len(client.calls))
        self.assertTrue(
            all(call[1:] == ("20260820", "20260821") for call in client.calls)
        )

        repeated = sync_board_daily_v1(
            client=_Client(), market_database_path=self.market,
            target_path=self.target, as_of="2026-08-21",
            fetched_at="2026-08-21T10:31:00+00:00",
        )
        self.assertEqual(0, repeated.inserted_row_count)
        with sqlite3.connect(self.target) as connection:
            repeated_count = connection.execute(
                "SELECT COUNT(*) FROM ths_board_daily WHERE trade_date='2026-08-21'"
            ).fetchone()[0]
        self.assertEqual(80, repeated_count)

    def test_rejects_incomplete_board_coverage_without_writing(self):
        with self.assertRaisesRegex(ValueError, "fewer than 80"):
            sync_board_daily_v1(
                client=_Client(79), market_database_path=self.market,
                target_path=self.target, as_of="2026-08-21",
                fetched_at="2026-08-21T10:30:00+00:00",
            )
        with sqlite3.connect(self.target) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM ths_board_daily WHERE trade_date='2026-08-21'"
            ).fetchone()[0]
        self.assertEqual(0, count)


if __name__ == "__main__":
    unittest.main()
