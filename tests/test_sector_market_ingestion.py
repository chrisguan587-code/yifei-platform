from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.sector_market_ingestion import (
    publish_sector_market_daily_v1,
    publish_sector_market_daily_v2,
)
from yifei_platform.supplemental_facts import (
    initialize_supplemental_database_v1,
)


class SectorMarketIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.market = root / "market.db"
        self.target = root / "supplemental.db"
        self.dates = [
            (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
            for offset in range(30)
        ]
        with sqlite3.connect(self.market) as connection:
            connection.executescript(
                """CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
                   CREATE TABLE stock_daily (
                       stock_code TEXT, trade_date TEXT,
                       pct_chg REAL, amount REAL
                   );"""
            )
            connection.executemany(
                "INSERT INTO trading_calendar VALUES (?)",
                ((day,) for day in self.dates),
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?,?,?,?)",
                (
                    (f"S{index:03d}", day, index / 100, 1_000 + index)
                    for day in self.dates for index in range(80)
                ),
            )
        initialize_supplemental_database_v1(self.target)
        with sqlite3.connect(self.target) as connection:
            connection.executemany(
                """INSERT INTO sector_membership_history (
                       stock_code,stock_name,sector_code,sector_name,
                       sector_level,valid_from,valid_to_exclusive,
                       source,source_version,fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        f"S{index:03d}", f"股票{index}", f"B{index:03d}",
                        f"行业{index}", "THS_L2", "2026-01-01", None,
                        "test", "test-membership.v1",
                        "2026-07-01T00:00:00+00:00",
                    )
                    for index in range(80)
                ),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publishes_thirty_days_and_repeated_run_is_idempotent(self) -> None:
        result = publish_sector_market_daily_v1(
            market_database_path=self.market,
            target_path=self.target,
            as_of=self.dates[-1],
            published_at="2026-07-30T10:00:00+00:00",
        )
        self.assertEqual(30, result.published_session_count)
        self.assertEqual(2_400, result.inserted_row_count)
        with sqlite3.connect(self.target) as connection:
            row = connection.execute(
                """SELECT member_count,observed_member_count,
                          equal_weight_return_pct,amount,coverage
                   FROM sector_market_daily
                   WHERE sector_code='B010' AND trade_date=?""",
                (self.dates[-1],),
            ).fetchone()
        self.assertEqual((1, 1, 0.1, 1010.0, 1.0), row)

        repeated = publish_sector_market_daily_v1(
            market_database_path=self.market,
            target_path=self.target,
            as_of=self.dates[-1],
            published_at="2026-07-30T10:01:00+00:00",
        )
        self.assertEqual(0, repeated.published_session_count)
        self.assertEqual(0, repeated.inserted_row_count)

    def test_rejects_partial_member_coverage_below_threshold(self) -> None:
        with sqlite3.connect(self.market) as connection:
            connection.execute(
                "DELETE FROM stock_daily WHERE trade_date=? AND stock_code<'S010'",
                (self.dates[-1],),
            )
        with self.assertRaisesRegex(ValueError, "member_coverage_below_0.95"):
            publish_sector_market_daily_v1(
                market_database_path=self.market,
                target_path=self.target,
                as_of=self.dates[-1],
                published_at="2026-07-30T10:00:00+00:00",
            )
        with sqlite3.connect(self.target) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM sector_market_daily"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_aggregates_multiple_members_and_rejects_zero_observation_sector(self) -> None:
        with sqlite3.connect(self.market) as connection:
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?,?,?,?)",
                (
                    ("S080", day, 1.0, 2_000.0)
                    for day in self.dates
                ),
            )
        with sqlite3.connect(self.target) as connection:
            connection.execute(
                """INSERT INTO sector_membership_history VALUES (
                       'S080','股票80','B000','行业0','THS_L2',
                       '2026-01-01',NULL,'test','test-membership.v1',
                       '2026-07-01T00:00:00+00:00')"""
            )
        publish_sector_market_daily_v1(
            market_database_path=self.market,
            target_path=self.target,
            as_of=self.dates[-1],
            published_at="2026-07-30T10:00:00+00:00",
        )
        with sqlite3.connect(self.target) as connection:
            row = connection.execute(
                """SELECT member_count,observed_member_count,
                          equal_weight_return_pct,amount
                   FROM sector_market_daily
                   WHERE sector_code='B000' AND trade_date=?""",
                (self.dates[-1],),
            ).fetchone()
        self.assertEqual((2, 2, 0.5, 3_000.0), row)

        with sqlite3.connect(self.market) as connection:
            connection.execute(
                "DELETE FROM stock_daily WHERE stock_code='S079'"
            )
        another = Path(self.temporary.name) / "missing-sector.db"
        initialize_supplemental_database_v1(another)
        with sqlite3.connect(self.target) as source, sqlite3.connect(another) as target:
            rows = source.execute(
                "SELECT * FROM sector_membership_history"
            ).fetchall()
            target.executemany(
                "INSERT INTO sector_membership_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )
        with self.assertRaisesRegex(ValueError, "sector_observation_missing"):
            publish_sector_market_daily_v1(
                market_database_path=self.market,
                target_path=another,
                as_of=self.dates[-1],
                published_at="2026-07-30T10:00:00+00:00",
            )

    def test_rejects_existing_incomplete_date_and_ambiguous_membership(self) -> None:
        with sqlite3.connect(self.target) as connection:
            connection.execute(
                """INSERT INTO sector_market_daily VALUES (
                       'B000','行业0','THS_L2',?,1,1,0.0,1000.0,'CNY',1.0,
                       'test','platform-stock-daily-ths-l2.v1',
                       'test-membership.v1','2026-07-30T10:00:00+00:00')""",
                (self.dates[-1],),
            )
        with self.assertRaisesRegex(ValueError, "existing_sector_market_incomplete"):
            publish_sector_market_daily_v1(
                market_database_path=self.market,
                target_path=self.target,
                as_of=self.dates[-1],
                published_at="2026-07-30T10:00:00+00:00",
            )

        with sqlite3.connect(self.target) as connection:
            connection.execute("DELETE FROM sector_market_daily")
            connection.execute(
                """INSERT INTO sector_membership_history VALUES (
                       'S000','股票0','BX','冲突行业','THS_L2',
                       '2026-01-01',NULL,'test','test-membership.v1',
                       '2026-07-01T00:00:00+00:00')"""
            )
        with self.assertRaisesRegex(ValueError, "ambiguous_sector_membership"):
            publish_sector_market_daily_v1(
                market_database_path=self.market,
                target_path=self.target,
                as_of=self.dates[-1],
                published_at="2026-07-30T10:00:00+00:00",
            )


class SwSectorMarketIngestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        self.market = root / "market.db"
        self.target = root / "supplemental.db"
        self.dates = [
            (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
            for offset in range(30)
        ]
        with sqlite3.connect(self.market) as connection:
            connection.executescript(
                """CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
                   CREATE TABLE stock_daily (
                       stock_code TEXT, trade_date TEXT,
                       pct_chg REAL, amount REAL
                   );"""
            )
            connection.executemany(
                "INSERT INTO trading_calendar VALUES (?)",
                ((day,) for day in self.dates),
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?,?,?,?)",
                (
                    (f"S{index:03d}", day, index / 100, 1_000.0)
                    for day in self.dates for index in range(131)
                ),
            )
        initialize_supplemental_database_v1(self.target)
        with sqlite3.connect(self.target) as connection:
            connection.executemany(
                """INSERT INTO sector_membership_history (
                       stock_code,stock_name,sector_code,sector_name,
                       sector_level,valid_from,valid_to_exclusive,
                       source,source_version,fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        f"S{index:03d}", f"股票{index}", f"SW{index:03d}",
                        f"申万行业{index}", "L2", "2026-01-01", None,
                        "test-cninfo", "test-cninfo-sw-l2.v1",
                        "2026-07-01T00:00:00+00:00",
                    )
                    for index in range(131)
                ),
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_publishes_sw_l2_v2_with_market_amount_coverage(self) -> None:
        result = publish_sector_market_daily_v2(
            market_database_path=self.market,
            target_path=self.target,
            as_of=self.dates[-1],
            published_at="2026-07-30T10:00:00+00:00",
        )
        self.assertEqual("L2", result.sector_level)
        self.assertEqual(25, result.published_session_count)
        self.assertEqual(3_275, result.inserted_row_count)
        self.assertEqual(1.0, result.market_amount_coverage)
        with sqlite3.connect(self.target) as connection:
            count, versions = connection.execute(
                """SELECT COUNT(*),COUNT(DISTINCT source_version)
                   FROM sector_market_daily WHERE sector_level='L2'"""
            ).fetchone()
        self.assertEqual((3_275, 1), (count, versions))

        repeated = publish_sector_market_daily_v2(
            market_database_path=self.market,
            target_path=self.target,
            as_of=self.dates[-1],
            published_at="2026-07-30T10:01:00+00:00",
        )
        self.assertEqual(0, repeated.inserted_row_count)
        self.assertEqual(1.0, repeated.market_amount_coverage)

    def test_rejects_market_amount_coverage_below_97_percent(self) -> None:
        with sqlite3.connect(self.target) as connection:
            connection.execute(
                """DELETE FROM sector_membership_history
                   WHERE sector_level='L2' AND stock_code<'S005'"""
            )
        with self.assertRaisesRegex(
            ValueError, "market amount coverage below 0.97"
        ):
            publish_sector_market_daily_v2(
                market_database_path=self.market,
                target_path=self.target,
                as_of=self.dates[-1],
                published_at="2026-07-30T10:00:00+00:00",
            )
        with sqlite3.connect(self.target) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM sector_market_daily"
            ).fetchone()[0]
        self.assertEqual(0, count)

    def test_rejects_supporting_history_amount_coverage_below_90_percent(self) -> None:
        with sqlite3.connect(self.market) as connection:
            connection.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?)",
                ("UNMAPPED", self.dates[-25], 0.0, 20_000.0),
            )
        with self.assertRaisesRegex(
            ValueError, "market amount coverage below 0.90"
        ):
            publish_sector_market_daily_v2(
                market_database_path=self.market,
                target_path=self.target,
                as_of=self.dates[-1],
                published_at="2026-07-30T10:00:00+00:00",
            )

    def test_repeated_run_rechecks_market_amount_coverage(self) -> None:
        publish_sector_market_daily_v2(
            market_database_path=self.market,
            target_path=self.target,
            as_of=self.dates[-1],
            published_at="2026-07-30T10:00:00+00:00",
        )
        with sqlite3.connect(self.market) as connection:
            connection.execute(
                "INSERT INTO stock_daily VALUES (?,?,?,?)",
                ("UNMAPPED", self.dates[-1], 0.0, 10_000.0),
            )
        with self.assertRaisesRegex(
            ValueError, "market amount coverage below 0.97"
        ):
            publish_sector_market_daily_v2(
                market_database_path=self.market,
                target_path=self.target,
                as_of=self.dates[-1],
                published_at="2026-07-30T10:01:00+00:00",
            )


if __name__ == "__main__":
    unittest.main()
