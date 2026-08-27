from pathlib import Path
import plistlib
import sqlite3
import tempfile
from unittest.mock import Mock, patch
import unittest

from yifei_platform.supplemental_cli import _publish_board_readiness


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

    def test_retry_reuses_existing_readiness_when_no_session_was_synced(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "supplemental.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE ths_board_daily (trade_date TEXT);"
                    "CREATE TABLE supplemental_metadata (key TEXT, value TEXT);"
                )
                connection.executemany(
                    "INSERT INTO ths_board_daily VALUES (?)",
                    (("2026-08-25",) for _ in range(90)),
                )
                connection.execute(
                    "INSERT INTO supplemental_metadata VALUES (?,?)",
                    ("board_source_version", "akshare-ths-industry.v1"),
                )
            existing = Mock(
                marker_id="existing-marker", bundle="v4-research-board",
                as_of="2026-08-25",
                producer_version="supplemental-market-facts.v1",
                required_datasets=("ths_board_daily",),
            )
            with patch(
                "yifei_platform.supplemental_cli.ReadinessStoreV1.read_ready",
                return_value=existing,
            ), patch(
                "yifei_platform.supplemental_cli.publish_supplemental_readiness_v1"
            ) as publish:
                marker = _publish_board_readiness(
                    database_path=database,
                    readiness_root=Path("state"),
                    as_of="2026-08-25",
                    published_at="2026-08-25T14:10:00+00:00",
                    source_version="akshare-ths-industry.v1",
                    synced_session_count=0,
                )

            self.assertIs(existing, marker)
            publish.assert_not_called()

    def test_retry_rejects_changed_board_source_version(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Path(temporary) / "supplemental.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    "CREATE TABLE ths_board_daily (trade_date TEXT);"
                    "CREATE TABLE supplemental_metadata (key TEXT, value TEXT);"
                )
                connection.executemany(
                    "INSERT INTO ths_board_daily VALUES (?)",
                    (("2026-08-25",) for _ in range(90)),
                )
                connection.execute(
                    "INSERT INTO supplemental_metadata VALUES (?,?)",
                    ("board_source_version", "old-source.v1"),
                )
            existing = Mock(
                bundle="v4-research-board", as_of="2026-08-25",
                producer_version="supplemental-market-facts.v1",
                required_datasets=("ths_board_daily",),
            )
            with patch(
                "yifei_platform.supplemental_cli.ReadinessStoreV1.read_ready",
                return_value=existing,
            ):
                with self.assertRaisesRegex(ValueError, "source version mismatch"):
                    _publish_board_readiness(
                        database_path=database,
                        readiness_root=Path("state"),
                        as_of="2026-08-25",
                        published_at="2026-08-25T14:10:00+00:00",
                        source_version="new-source.v2",
                        synced_session_count=0,
                    )


if __name__ == "__main__":
    unittest.main()
