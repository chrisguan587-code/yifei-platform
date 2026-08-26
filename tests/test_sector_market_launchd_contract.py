from pathlib import Path
import plistlib
import unittest


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


if __name__ == "__main__":
    unittest.main()
