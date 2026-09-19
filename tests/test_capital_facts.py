from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.capital_facts import (
    FloatShareSourceRowV1,
    publish_float_market_cap_daily_v1,
    publish_float_shares_weekly_v1,
)


class FakeFloatShareClient:
    source = "fake.float_shares"
    source_version = "fake.v1"

    def __init__(self, rows: dict[str, float], *, source_date: str = "2026-09-13") -> None:
        self._rows = rows
        self._source_date = source_date

    def read_float_shares(self, stock_codes):
        return {
            code: FloatShareSourceRowV1(
                stock_code=code,
                float_shares=value,
                source_date=self._source_date,
            )
            for code, value in self._rows.items()
            if code in stock_codes
        }


class SharedCapitalFactsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.market_db = self.root / "market_data.db"
        self.shared_db = self.root / "supplemental_facts.db"
        self._seed_market_db()

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_weekly_float_shares_publish_uses_shared_db_and_flags_backup_diff(self) -> None:
        primary = FakeFloatShareClient({
            "000001": 1000,
            "000002": 2000,
            "300001": 3000,
        })
        backup = FakeFloatShareClient({
            "000001": 1000,
            "000002": 2600,
            "300001": 3000,
        })

        result = publish_float_shares_weekly_v1(
            client=primary,
            backup_client=backup,
            backup_sample_size=3,
            market_database_path=self.market_db,
            target_path=self.shared_db,
            as_of="2026-09-17",
            updated_at="2026-09-17T09:00:00+00:00",
        )

        self.assertEqual("float_shares", result.dataset)
        self.assertEqual(3, result.row_count)
        self.assertEqual(3, result.expected_count)
        self.assertEqual("READY", result.health_status)
        with sqlite3.connect(self.shared_db) as connection:
            rows = connection.execute(
                "SELECT stock_code,float_shares,share_unit,source,health_status "
                "FROM stock_float_shares ORDER BY stock_code"
            ).fetchall()
            self.assertEqual(3, len(rows))
            self.assertEqual(("000001", 1000.0, "SHARE", "fake.float_shares", "READY"), rows[0])
            anomalies = connection.execute(
                "SELECT stock_code,reason FROM capital_float_share_anomalies"
            ).fetchall()
            self.assertIn(("000002", "backup_diff_gt_threshold"), anomalies)

    def test_daily_float_market_cap_uses_latest_effective_float_shares(self) -> None:
        publish_float_shares_weekly_v1(
            client=FakeFloatShareClient({
                "000001": 1000,
                "000002": 2000,
                "300001": 3000,
            }),
            backup_client=None,
            market_database_path=self.market_db,
            target_path=self.shared_db,
            as_of="2026-09-17",
            updated_at="2026-09-17T09:00:00+00:00",
        )

        result = publish_float_market_cap_daily_v1(
            market_database_path=self.market_db,
            target_path=self.shared_db,
            as_of="2026-09-17",
            updated_at="2026-09-17T09:30:00+00:00",
        )

        self.assertEqual("float_market_cap", result.dataset)
        self.assertEqual(3, result.row_count)
        self.assertEqual("READY", result.health_status)
        with sqlite3.connect(self.shared_db) as connection:
            rows = connection.execute(
                "SELECT stock_code,trade_date,float_shares_as_of,close,float_market_cap "
                "FROM stock_float_market_cap_daily ORDER BY stock_code"
            ).fetchall()
            self.assertEqual(("000001", "2026-09-17", "2026-09-17", 10.0, 10000.0), rows[0])
            quality = connection.execute(
                "SELECT dataset,coverage,health_status FROM capital_data_quality "
                "WHERE dataset='float_market_cap'"
            ).fetchone()
            self.assertEqual(("float_market_cap", 1.0, "READY"), quality)

    def test_missing_float_shares_degrades_without_estimation(self) -> None:
        result = publish_float_shares_weekly_v1(
            client=FakeFloatShareClient({"000001": 1000}),
            backup_client=None,
            market_database_path=self.market_db,
            target_path=self.shared_db,
            as_of="2026-09-17",
            updated_at="2026-09-17T09:00:00+00:00",
        )

        self.assertEqual("DEGRADED", result.health_status)
        self.assertEqual(1, result.row_count)
        self.assertEqual(2, result.missing_count)
        with sqlite3.connect(self.shared_db) as connection:
            missing = connection.execute(
                "SELECT stock_code,reason FROM capital_float_share_missing ORDER BY stock_code"
            ).fetchall()
            self.assertEqual([
                ("000002", "missing_source_row"),
                ("300001", "missing_source_row"),
            ], missing)

    def _seed_market_db(self) -> None:
        with sqlite3.connect(self.market_db) as connection:
            connection.execute(
                """CREATE TABLE stock_daily (
                    stock_code TEXT, stock_name TEXT, trade_date TEXT,
                    close REAL, is_st INTEGER
                )"""
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?,?,?,?,?)",
                [
                    ("000001", "平安银行", "2026-09-17", 10.0, 0),
                    ("000002", "万 科Ａ", "2026-09-17", 20.0, 0),
                    ("300001", "特锐德", "2026-09-17", 30.0, 0),
                    ("688001", "科创样本", "2026-09-17", 40.0, 0),
                    ("920001", "北交样本", "2026-09-17", 50.0, 0),
                    ("000003", "ST样本", "2026-09-17", 60.0, 1),
                ],
            )


if __name__ == "__main__":
    unittest.main()
