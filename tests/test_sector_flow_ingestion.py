from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.board_capital import CapitalFactReaderV1
from yifei_platform.market_data import ReadStatus
from yifei_platform.sector_flow_ingestion import (
    publish_sector_flow_daily_v1,
)


class SectorFlowIngestionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.market = self.root / "market.db"
        self.target = self.root / "supplemental.db"
        self.raw = self.root / "raw"
        with sqlite3.connect(self.market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.execute(
                "INSERT INTO stock_daily VALUES ('000001', '2026-07-27')"
            )

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_publishes_cny_facts_and_immutable_raw_snapshot(self) -> None:
        result = self._publish(_FakeSectorFlowClient())
        self.assertEqual(2, result.sector_count)
        snapshot = json.loads(
            result.raw_snapshot_path.read_text(encoding="utf-8")
        )
        self.assertEqual(
            {"amount": "CNY", "main_inflow": "CNY"},
            snapshot["consumed_units"],
        )
        self.assertEqual(
            "unit_not_audited",
            snapshot["unconsumed_fields"]["volume"],
        )
        read = CapitalFactReaderV1(
            self.target, source_version=result.source_version
        ).read_sector_daily("2026-07-27")
        self.assertTrue(read.ok)
        self.assertEqual(2, len(read.facts))
        self.assertEqual(20_000_000, read.facts[0].main_inflow)
        self.assertEqual("CNY", read.facts[0].amount_unit)
        self.assertEqual("CNY", read.facts[0].main_inflow_unit)
        with sqlite3.connect(self.target) as connection:
            metadata = dict(connection.execute(
                "SELECT key, value FROM supplemental_metadata"
            ))
        self.assertEqual("CNY", metadata["sector_flow_amount_unit"])
        self.assertEqual(
            "not_consumed_unit_unaudited",
            metadata["sector_flow_volume_status"],
        )

    def test_same_batch_is_idempotent_but_changed_snapshot_conflicts(self) -> None:
        first = self._publish(_FakeSectorFlowClient())
        before = self.target.read_bytes()
        repeated = self._publish(_FakeSectorFlowClient())
        self.assertEqual(first.raw_snapshot_path, repeated.raw_snapshot_path)
        self.assertEqual(before, self.target.read_bytes())
        with self.assertRaisesRegex(ValueError, "already published|snapshot"):
            self._publish(_ChangedSectorFlowClient())
        self.assertEqual(before, self.target.read_bytes())

    def test_database_conflict_is_detected_before_raw_snapshot_publish(self) -> None:
        self._publish(_FakeSectorFlowClient())
        snapshot = self.raw / "sector_em" / "2026-07-27.json"
        snapshot.unlink()
        before = self.target.read_bytes()

        with self.assertRaisesRegex(ValueError, "already published"):
            self._publish(_ChangedSectorFlowClient())

        self.assertEqual(before, self.target.read_bytes())
        self.assertFalse(snapshot.exists())

    def test_unit_mismatch_blocks_before_snapshot_or_database(self) -> None:
        with self.assertRaisesRegex(ValueError, "unit mismatch"):
            self._publish(_MismatchedSectorFlowClient(), minimum=1)
        self.assertFalse(self.target.exists())
        self.assertFalse((self.raw / "sector_em" / "2026-07-27.json").exists())

    def test_current_day_source_rejects_backdating_and_intraday_publish(self) -> None:
        with self.assertRaisesRegex(ValueError, "current-day only"):
            publish_sector_flow_daily_v1(
                client=_FakeSectorFlowClient(),
                market_database_path=self.market,
                target_path=self.target,
                raw_snapshot_root=self.raw,
                as_of="2026-07-26",
                fetched_at="2026-07-27T15:20:00+08:00",
                minimum_sector_count=2,
                observed_now=datetime.fromisoformat(
                    "2026-07-27T15:20:00+08:00"
                ),
            )

    def test_reader_blocks_when_monetary_unit_columns_are_missing(self) -> None:
        legacy = self.root / "legacy-sector.db"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                """CREATE TABLE sector_fund_flow_daily (
                    trade_date TEXT, sector_code TEXT,
                    source_version TEXT
                )"""
            )
            connection.execute(
                """INSERT INTO sector_fund_flow_daily
                   VALUES ('2026-07-27','BK001',?)""",
                ("levistock.eastmoney-sector-em.industry.v1",),
            )
        result = CapitalFactReaderV1(
            legacy,
            source_version="levistock.eastmoney-sector-em.industry.v1",
        ).read_sector_daily("2026-07-27")
        self.assertEqual(ReadStatus.BLOCKED, result.status)
        self.assertIn(
            "required_column_missing:amount_unit",
            result.reason_codes,
        )
        with self.assertRaisesRegex(ValueError, "after 15:10"):
            publish_sector_flow_daily_v1(
                client=_FakeSectorFlowClient(),
                market_database_path=self.market,
                target_path=self.target,
                raw_snapshot_root=self.raw,
                as_of="2026-07-27",
                fetched_at="2026-07-27T14:59:00+08:00",
                minimum_sector_count=2,
                observed_now=datetime.fromisoformat(
                    "2026-07-27T14:59:00+08:00"
                ),
            )

    def _publish(self, client, *, minimum: int = 2):
        return publish_sector_flow_daily_v1(
            client=client,
            market_database_path=self.market,
            target_path=self.target,
            raw_snapshot_root=self.raw,
            as_of="2026-07-27",
            fetched_at="2026-07-27T15:20:00+08:00",
            minimum_sector_count=minimum,
            observed_now=datetime.fromisoformat(
                "2026-07-27T15:20:00+08:00"
            ),
        )


class _FakeSectorFlowClient:
    def read_industry(self):
        return (
            {
                "sector_code": "BK001",
                "sector_name": "银行",
                "amount": 100_000_000,
                "change_pct": 1.2,
                "main_inflow": 20_000_000,
                "up_count": 20,
                "down_count": 5,
                "lead_stock_name": "甲",
                "lead_stock_chg": 3.2,
                "volume": 1234,
            },
            {
                "sector_code": "BK002",
                "sector_name": "证券",
                "amount": 80_000_000,
                "change_pct": -0.2,
                "main_inflow": -5_000_000,
                "up_count": 4,
                "down_count": 16,
                "lead_stock_name": "乙",
                "lead_stock_chg": None,
                "volume": 5678,
            },
        )


class _ChangedSectorFlowClient(_FakeSectorFlowClient):
    def read_industry(self):
        rows = [dict(row) for row in super().read_industry()]
        rows[0]["main_inflow"] = 21_000_000
        return tuple(rows)


class _MismatchedSectorFlowClient(_FakeSectorFlowClient):
    def read_industry(self):
        row = dict(super().read_industry()[0])
        row["main_inflow"] = 10_000_000_000
        return (row,)


if __name__ == "__main__":
    unittest.main()
