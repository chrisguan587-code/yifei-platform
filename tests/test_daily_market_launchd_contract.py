from pathlib import Path
import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class DailyMarketLaunchdContractTest(unittest.TestCase):
    def test_schedule_uses_platform_paths_and_explicit_exchange_calendar(self) -> None:
        script = (ROOT / "scripts/run_daily_market.sh").read_text(encoding="utf-8")
        plist_path = ROOT / "ops/launchd/com.yplus.yifei-platform.daily-market.plist"
        with plist_path.open("rb") as stream:
            plist = plistlib.load(stream)
        combined = script + str(plist)

        self.assertNotIn("yifei_V3", combined)
        self.assertNotIn("source-health", combined)
        self.assertIn("exchange_calendar", combined)
        self.assertLess(script.index('as_of in payload["sessions"]'),
                        script.index('while [ "$attempt" -le 3 ]'))
        self.assertEqual(17, plist["StartCalendarInterval"]["Hour"])
        self.assertEqual(30, plist["StartCalendarInterval"]["Minute"])

    def test_non_session_exits_before_publisher(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            calendar = root / "calendar.json"
            calendar.write_text(json.dumps({
                "schema_version": "exchange-trading-calendar.v1",
                "sessions": ["2026-08-24"],
            }), encoding="utf-8")
            environment = os.environ.copy()
            environment["AS_OF"] = "2026-08-23"
            environment["YIFEI_PLATFORM_PYTHON"] = sys.executable
            result = subprocess.run(
                [
                    "/bin/sh", str(ROOT / "scripts/run_daily_market.sh"),
                    str(calendar), str(root / "missing.db"), str(root / "state"),
                ],
                env=environment, capture_output=True, text=True, check=False,
            )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertIn("not an exchange session", result.stdout)


if __name__ == "__main__":
    unittest.main()
