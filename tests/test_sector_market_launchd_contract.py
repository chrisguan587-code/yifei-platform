from pathlib import Path
import plistlib
import sqlite3
import tempfile
import unittest

from yifei_platform.supplemental_cli import (
    _publish_sector_market_readiness,
    _publish_sector_market_v2_readiness,
)
from yifei_platform.supplemental_facts import (
    initialize_supplemental_database_v1,
)


ROOT = Path(__file__).resolve().parents[1]


class SectorMarketLaunchdContractTests(unittest.TestCase):
    def test_runs_early_and_waits_for_authoritative_market_facts(self) -> None:
        plist_path = (
            ROOT / "ops/launchd/com.yplus.yifei-platform.sector-market-daily.plist"
        )
        with plist_path.open("rb") as stream:
            payload = plistlib.load(stream)
        intervals = payload["StartCalendarInterval"]
        self.assertEqual({2, 3, 4, 5, 6}, {item["Weekday"] for item in intervals})
        self.assertTrue(
            all(item["Hour"] == 17 and item["Minute"] == 40 for item in intervals)
        )
        arguments = payload["ProgramArguments"]
        self.assertTrue(arguments[1].endswith("run_sector_market_daily.sh"))
        self.assertNotIn("yifei_V3", " ".join(arguments))

        script = (ROOT / "scripts/run_sector_market_daily.sh").read_text(
            encoding="utf-8"
        )
        self.assertIn("stock_daily", script)
        self.assertIn("market_breadth_daily", script)
        self.assertIn('while [ "$attempt" -lt 480 ]', script)
        self.assertIn("publish-sector-market-daily", script)
        self.assertIn("publish-sector-market-daily-v2", script)
        self.assertIn('status=0', script)
        self.assertIn('exit "$status"', script)

    def test_publishes_neutral_readiness_for_both_taxonomies(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            database = root / "supplemental.db"
            readiness = root / "state"
            initialize_supplemental_database_v1(database)
            with sqlite3.connect(database) as connection:
                connection.executemany(
                    "INSERT INTO sector_market_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        (
                            f"THS{index:03d}", f"同花顺行业{index}", "THS_L2",
                            "2026-08-28", 1, 1, 0.0, 1.0, "CNY", 1.0,
                            "platform.stock_daily+sector_membership_history",
                            "platform-stock-daily-ths-l2.v1", "membership.v1",
                            "2026-08-28T10:00:00+00:00",
                        )
                        for index in range(80)
                    ),
                )
                connection.executemany(
                    "INSERT INTO sector_market_daily VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        (
                            f"SW{index:03d}", f"申万行业{index}", "L2",
                            "2026-08-28", 1, 1, 0.0, 1.0, "CNY", 1.0,
                            "platform.stock_daily+sector_membership_history",
                            "platform-stock-daily-cninfo-sw-l2.v2",
                            "membership.v2", "2026-08-28T10:00:00+00:00",
                        )
                        for index in range(120)
                    ),
                )

            ths = _publish_sector_market_readiness(
                database_path=database, readiness_root=readiness,
                as_of="2026-08-28",
                published_at="2026-08-28T10:00:00+00:00",
                source_version="platform-stock-daily-ths-l2.v1",
                published_session_count=1, bundle="sector-market-ths-l2",
                dataset="sector_market_daily_ths_l2",
                sector_level="THS_L2", minimum_sector_count=80,
            )
            sw = _publish_sector_market_v2_readiness(
                database_path=database, readiness_root=readiness,
                as_of="2026-08-28",
                published_at="2026-08-28T10:00:00+00:00",
                source_version="platform-stock-daily-cninfo-sw-l2.v2",
                published_session_count=1,
            )

            self.assertEqual("sector-market-ths-l2", ths.bundle)
            self.assertEqual(
                ("sector_market_daily_ths_l2",), ths.required_datasets
            )
            self.assertEqual("sector-market-sw-l2", sw.bundle)
            self.assertEqual(
                ("sector_market_daily_sw_l2",), sw.required_datasets
            )


if __name__ == "__main__":
    unittest.main()
