from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from contextlib import redirect_stdout
import io
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from yifei_platform.market_observation import (
    append_market_observation_facts_v1,
    migrate_market_observation_facts_v1,
)
from yifei_platform.market_observation_cli import main as migration_main


class MarketObservationMigrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.target = self.root / "market.db"
        self.legacy = self.root / "legacy.db"
        self.sessions = [
            (date(2026, 7, 1) + timedelta(days=offset)).isoformat()
            for offset in range(21)
        ]
        with sqlite3.connect(self.target) as connection:
            connection.executescript("""
                CREATE TABLE stock_daily (
                    stock_code TEXT NOT NULL, stock_name TEXT, trade_date TEXT NOT NULL,
                    open REAL, high REAL, low REAL, close REAL, preclose REAL,
                    volume REAL, amount REAL, pct_chg REAL, turnover REAL, is_st INTEGER,
                    PRIMARY KEY (stock_code, trade_date)
                );
                CREATE TABLE trading_calendar (trade_date TEXT PRIMARY KEY);
                CREATE TABLE platform_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                INSERT INTO platform_metadata VALUES
                    ('schema_version','market-data.bootstrap.v1'),
                    ('producer_version','bootstrap-market-data.v1'),
                    ('published_at','2026-07-21T18:00:00+08:00');
            """)
            connection.executemany(
                "INSERT INTO trading_calendar VALUES (?)",
                ((session,) for session in self.sessions),
            )
            rows = []
            for offset, session in enumerate(self.sessions):
                for code, direction in (("000001", 1), ("000002", -1)):
                    preclose = 10 + direction * offset * 0.1
                    close = preclose + direction * 0.1
                    rows.append((
                        code, code, session, preclose, close, close, close,
                        preclose, 100.0, 1000.0, direction, None, 0,
                    ))
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows
            )
        with sqlite3.connect(self.legacy) as connection:
            connection.execute("""
                CREATE TABLE index_daily (
                    date TEXT, open REAL, close REAL, high REAL, low REAL,
                    volume INTEGER, amount REAL
                )
            """)
            connection.executemany(
                "INSERT INTO index_daily VALUES (?,?,?,?,?,?,?)",
                (
                    (session, 100 + offset, 101 + offset, 102 + offset,
                     99 + offset, 1000 + offset, None)
                    for offset, session in enumerate(self.sessions)
                ),
            )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_migrates_index_and_derives_breadth_without_application_semantics(self) -> None:
        result = migrate_market_observation_facts_v1(
            target_path=self.target,
            legacy_index_path=self.legacy,
            published_at="2026-07-21T19:00:00+08:00",
        )

        self.assertEqual(21, result.index_row_count)
        self.assertEqual(21, result.breadth_row_count)
        with sqlite3.connect(self.target) as connection:
            index_row = connection.execute(
                "SELECT preclose,return_20d_pct,realized_vol_10d_pct "
                "FROM index_daily ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            breadth = connection.execute(
                "SELECT advance_count,decline_count,valid_return_count,"
                "ma20_eligible_stock_count,amount_ratio_vs_prior20_median "
                "FROM market_breadth_daily ORDER BY trade_date DESC LIMIT 1"
            ).fetchone()
            columns = {
                row[1] for row in connection.execute(
                    "PRAGMA table_info(market_breadth_daily)"
                )
            }
        self.assertEqual(120.0, index_row[0])
        self.assertIsNotNone(index_row[1])
        self.assertIsNotNone(index_row[2])
        self.assertEqual((1, 1, 2, 2, 1.0), breadth)
        self.assertTrue({"trade_date", "advance_share", "source_version"} <= columns)
        self.assertFalse({"label", "score", "recommendation"} & columns)

        with self.assertRaisesRegex(FileExistsError, "already migrated"):
            migrate_market_observation_facts_v1(
                target_path=self.target,
                legacy_index_path=self.legacy,
                published_at="2026-07-21T20:00:00+08:00",
            )

    def test_stale_index_does_not_block_complete_breadth_migration(self) -> None:
        with sqlite3.connect(self.legacy) as connection:
            connection.execute(
                "DELETE FROM index_daily WHERE date=?", (self.sessions[-1],)
            )

        result = migrate_market_observation_facts_v1(
            target_path=self.target,
            legacy_index_path=self.legacy,
            published_at="2026-07-21T19:00:00+08:00",
        )

        self.assertEqual(20, result.index_row_count)
        self.assertEqual(21, result.breadth_row_count)

    def test_cli_does_not_block_breadth_when_exact_index_fetch_fails(self) -> None:
        with sqlite3.connect(self.legacy) as connection:
            connection.execute(
                "DELETE FROM index_daily WHERE date=?", (self.sessions[-1],)
            )
        arguments = [
            "yifei-platform-migrate-market-observation",
            "--target-db", str(self.target),
            "--legacy-index-db", str(self.legacy),
            "--published-at", "2026-07-21T19:00:00+08:00",
        ]

        with patch.object(sys, "argv", arguments), patch(
            "yifei_platform.market_observation_cli."
            "AkshareCsi300DailyClientV1.fetch",
            side_effect=RuntimeError("network unavailable"),
        ), redirect_stdout(io.StringIO()):
            self.assertEqual(0, migration_main())

        with sqlite3.connect(self.target) as connection:
            self.assertEqual(
                21,
                connection.execute(
                    "SELECT COUNT(*) FROM market_breadth_daily"
                ).fetchone()[0],
            )

    def test_historical_ma20_requires_consecutive_market_sessions(self) -> None:
        with sqlite3.connect(self.target) as connection:
            connection.execute(
                "DELETE FROM stock_daily WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[10],),
            )

        migrate_market_observation_facts_v1(
            target_path=self.target,
            legacy_index_path=self.legacy,
            published_at="2026-07-21T19:00:00+08:00",
        )

        with sqlite3.connect(self.target) as connection:
            eligible = connection.execute(
                "SELECT ma20_eligible_stock_count FROM market_breadth_daily "
                "WHERE trade_date=?",
                (self.sessions[-1],),
            ).fetchone()[0]
        self.assertEqual(1, eligible)

    def test_historical_thresholds_use_close_and_preclose_not_vendor_rounding(self) -> None:
        with sqlite3.connect(self.target) as connection:
            connection.execute(
                "UPDATE stock_daily SET close=10.2,preclose=10,pct_chg=9 "
                "WHERE stock_code='000001' AND trade_date=?",
                (self.sessions[-1],),
            )

        migrate_market_observation_facts_v1(
            target_path=self.target,
            legacy_index_path=self.legacy,
            published_at="2026-07-21T19:00:00+08:00",
        )

        with sqlite3.connect(self.target) as connection:
            share = connection.execute(
                "SELECT pct_ge_3_share FROM market_breadth_daily WHERE trade_date=?",
                (self.sessions[-1],),
            ).fetchone()[0]
        self.assertEqual(0.0, share)

    def test_amount_ratio_requires_the_exact_prior_20_sessions(self) -> None:
        extra_session = "2026-07-22"
        with sqlite3.connect(self.target) as connection:
            connection.execute("INSERT INTO trading_calendar VALUES (?)", (extra_session,))
            connection.execute(
                "UPDATE stock_daily SET amount=0 WHERE trade_date=?",
                (self.sessions[10],),
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    (code, code, extra_session, 10, 10, 10, close, 10,
                     100, 1000, (close / 10 - 1) * 100, None, 0)
                    for code, close in (("000001", 10.1), ("000002", 9.9))
                ),
            )
        with sqlite3.connect(self.legacy) as connection:
            connection.execute(
                "INSERT INTO index_daily VALUES (?,?,?,?,?,?,?)",
                (extra_session, 121, 122, 123, 120, 1021, None),
            )

        migrate_market_observation_facts_v1(
            target_path=self.target,
            legacy_index_path=self.legacy,
            published_at="2026-07-22T19:00:00+08:00",
        )

        with sqlite3.connect(self.target) as connection:
            ratio = connection.execute(
                "SELECT amount_ratio_vs_prior20_median FROM market_breadth_daily "
                "WHERE trade_date=?",
                (extra_session,),
            ).fetchone()[0]
            connection.execute(
                "DELETE FROM market_breadth_daily WHERE trade_date=?",
                (extra_session,),
            )
            append_market_observation_facts_v1(
                connection,
                as_of=extra_session,
                index_row=None,
                index_source_version=None,
            )
            daily_ratio = connection.execute(
                "SELECT amount_ratio_vs_prior20_median FROM market_breadth_daily "
                "WHERE trade_date=?",
                (extra_session,),
            ).fetchone()[0]
        self.assertIsNone(ratio)
        self.assertIsNone(daily_ratio)


if __name__ == "__main__":
    unittest.main()
