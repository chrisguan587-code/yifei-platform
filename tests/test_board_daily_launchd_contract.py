from pathlib import Path
import plistlib
import unittest


ROOT = Path(__file__).resolve().parents[1]


class BoardDailyLaunchdContractTest(unittest.TestCase):
    def test_publisher_waits_for_authoritative_stock_day_before_sync(self) -> None:
        script = (ROOT / "scripts/run_board_daily.sh").read_text(encoding="utf-8")

        wait_position = script.index(
            "SELECT 1 FROM stock_daily WHERE trade_date=? LIMIT 1"
        )
        sync_position = script.index("sync-board-daily")
        self.assertLess(wait_position, sync_position)
        self.assertIn('while [ "$attempt" -lt 80 ]', script)
        self.assertIn("exit 75", script)

    def test_schedule_runs_after_ths_close_data_and_retries_once(self) -> None:
        plist_path = ROOT / "ops/launchd/com.yplus.yifei-platform.board-daily.plist"
        with plist_path.open("rb") as stream:
            payload = plistlib.load(stream)

        self.assertEqual(
            [
                {"Hour": 21, "Minute": 10},
                {"Hour": 22, "Minute": 10},
            ],
            payload["StartCalendarInterval"],
        )
        self.assertNotIn("yifei_V3", str(payload))


if __name__ == "__main__":
    unittest.main()
