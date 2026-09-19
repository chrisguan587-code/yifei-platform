from pathlib import Path
import plistlib
import unittest

ROOT = Path(__file__).resolve().parents[1]


class CapitalFactsLaunchdContractTest(unittest.TestCase):
    def test_capital_jobs_use_platform_code_and_shared_data_paths(self) -> None:
        weekly = ROOT / "ops/launchd/com.yplus.yifei-platform.capital-float-shares-weekly.plist"
        daily = ROOT / "ops/launchd/com.yplus.yifei-platform.capital-market-cap-daily.plist"
        for path in (weekly, daily):
            with path.open("rb") as stream:
                payload = plistlib.load(stream)
            combined = " ".join(str(item) for item in payload["ProgramArguments"])
            self.assertIn("/Users/y-plus/projects/yifei/yifei-platform/scripts/run_capital_facts.sh", combined)
            self.assertIn("/Users/y-plus/projects/yifei/data/shared/market_data.db", combined)
            self.assertIn("/Users/y-plus/projects/yifei/data/shared/supplemental_facts.db", combined)
            self.assertNotIn("yifei-shortline", combined)
            self.assertNotIn("yifei_V3.1.0", combined)
            self.assertNotIn("yifei-v4", combined)
        with weekly.open("rb") as stream:
            weekly_payload = plistlib.load(stream)
        self.assertEqual(5, weekly_payload["StartCalendarInterval"]["Weekday"])
        self.assertEqual(18, weekly_payload["StartCalendarInterval"]["Hour"])
        self.assertEqual(35, weekly_payload["StartCalendarInterval"]["Minute"])


if __name__ == "__main__":
    unittest.main()
