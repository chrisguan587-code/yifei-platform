from pathlib import Path
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


if __name__ == "__main__":
    unittest.main()
