from __future__ import annotations

from pathlib import Path
import json
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from yifei_platform.public_data_ingestion import BaoStockDailyClientV1
from yifei_platform.bootstrap import (
    TURNOVER_ENRICHED_DAILY_VERSION,
    load_market_metadata,
    publish_turnover_enriched_daily_market_data,
)
from yifei_platform.turnover_ingestion import (
    BAOSTOCK_TURNOVER_SOURCE_VERSION,
    build_derived_turnover_snapshot_v1,
    build_float_share_reference_v1,
    build_baostock_turnover_snapshot_v1,
    write_turnover_snapshot_v1,
)
from yifei_platform.turnover_cli import main as turnover_cli_main


class TurnoverDailyPublisherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.source = self.root / "legacy.db"
        self.target = self.root / "market_data.db"
        self.readiness = self.root / "readiness"
        self.health = self.root / "health.json"
        self.snapshot = self.root / "turnover.json"
        self.capital = self.root / "capital.db"
        with sqlite3.connect(self.source) as connection:
            connection.executescript("""
                CREATE TABLE stock_daily (
                    stock_code TEXT, stock_name TEXT, trade_date TEXT, open REAL,
                    high REAL, low REAL, close REAL, preclose REAL, volume REAL,
                    amount REAL, pct_chg REAL, turnover REAL, is_st INTEGER,
                    PRIMARY KEY (stock_code, trade_date)
                );
                INSERT INTO stock_daily VALUES
                    ('000001','A','2026-07-27',10,11,9,10,10,900,9000,0,NULL,0),
                    ('600000','B','2026-07-27',20,21,19,20,20,1800,36000,0,NULL,0),
                    ('000001','A','2026-07-28',10,12,9,11,10,1000,11000,10,NULL,0),
                    ('600000','B','2026-07-28',20,21,19,20,20,2000,40000,0,NULL,0);
            """)
        with sqlite3.connect(self.capital) as connection:
            connection.executescript("""
                CREATE TABLE supplemental_metadata (
                    key TEXT PRIMARY KEY, value TEXT NOT NULL
                );
                CREATE TABLE stock_capital_daily (
                    stock_code TEXT, trade_date TEXT, float_market_cap REAL
                );
                INSERT INTO supplemental_metadata VALUES
                    ('capital_amount_unit','CNY'),
                    ('capital_turnover_raw_unit','PERCENT'),
                    ('capital_volume_raw_unit','SHARE'),
                    ('capital_source','sina.moneyflow.r0+baostock'),
                    ('capital_source_version','sina-moneyflow-r0+baostock-daily.v2'),
                    ('capital_fetched_at','2026-07-28T17:00:00+08:00');
                INSERT INTO stock_capital_daily VALUES
                    ('000001','2026-07-27',100000000),
                    ('600000','2026-07-27',200000000);
            """)
        self.health.write_text(json.dumps({
            "trade_date": "2026-07-28",
            "stock_daily_date": "2026-07-28",
            "stock_daily_rows": 2,
            "status": "success",
            "final_gate": "ok",
        }), encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_builds_snapshot_with_explicit_units_and_atomic_idempotency(self) -> None:
        payload = build_baostock_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            client=_FakeBaoStockClient(),
        )

        self.assertEqual(2, payload["summary"]["eligible_row_count"])
        self.assertEqual(1.0, payload["summary"]["coverage"])
        self.assertEqual("SHARE", payload["units"]["volume"])
        self.assertEqual("CNY", payload["units"]["amount"])
        self.assertEqual("PERCENT", payload["units"]["turnover"])
        write_turnover_snapshot_v1(payload=payload, output=self.snapshot)
        first = self.snapshot.read_bytes()
        write_turnover_snapshot_v1(payload=payload, output=self.snapshot)
        self.assertEqual(first, self.snapshot.read_bytes())

    def test_accepts_amounts_rounded_to_the_nearest_yuan(self) -> None:
        payload = build_baostock_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            client=_FakeBaoStockClient(amount_offset=-0.5),
        )
        self.assertEqual(1.0, payload["summary"]["coverage"])

        with self.assertRaisesRegex(ValueError, "amount does not match"):
            build_baostock_turnover_snapshot_v1(
                market_database_path=self.source,
                as_of="2026-07-28",
                fetched_at="2026-07-28T17:40:00+08:00",
                client=_FakeBaoStockClient(amount_offset=-1.01),
            )

    def test_concurrent_different_snapshot_cannot_replace_first_writer(self) -> None:
        payload = build_baostock_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            client=_FakeBaoStockClient(),
        )

        def competing_writer(_temporary, output):
            Path(output).write_text("{}\n", encoding="utf-8")
            raise FileExistsError

        with patch(
            "yifei_platform.turnover_ingestion.os.link",
            side_effect=competing_writer,
        ):
            with self.assertRaisesRegex(FileExistsError, "different content"):
                write_turnover_snapshot_v1(
                    payload=payload,
                    output=self.snapshot,
                )
        self.assertEqual("{}\n", self.snapshot.read_text(encoding="utf-8"))

    def test_baostock_login_retries_before_succeeding(self) -> None:
        attempts = []
        fake_module = types.SimpleNamespace(
            login=lambda: _login_result(attempts),
            logout=lambda: None,
        )
        with (
            patch.dict(sys.modules, {"baostock": fake_module}),
            patch("yifei_platform.public_data_ingestion.time.sleep"),
        ):
            client = BaoStockDailyClientV1(retry_attempts=3)
            client.close()
        self.assertEqual(3, len(attempts))

    def test_cli_distinguishes_transient_source_failure_from_identity_error(
        self,
    ) -> None:
        argv = [
            "turnover",
            "--market-db", str(self.source),
            "--as-of", "2026-07-28",
            "--fetched-at", "2026-07-28T17:40:00+08:00",
            "--output", str(self.snapshot),
        ]
        with (
            patch.object(sys, "argv", argv),
            patch(
                "yifei_platform.turnover_cli.BaoStockDailyClientV1",
                side_effect=RuntimeError("network unavailable"),
            ),
        ):
            self.assertEqual(75, turnover_cli_main())

        self._write_snapshot(_FakeBaoStockClient())
        with (
            patch.object(
                sys,
                "argv",
                argv + [
                    "--float-share-reference",
                    str(self.root / "reference.json"),
                ],
            ),
            self.assertRaises(SystemExit) as raised,
        ):
            turnover_cli_main()
        self.assertEqual(2, raised.exception.code)

    def test_existing_exact_snapshot_rejects_changed_market_database(
        self,
    ) -> None:
        self._write_snapshot(_FakeBaoStockClient())
        with sqlite3.connect(self.source) as connection:
            connection.execute(
                """UPDATE stock_daily SET amount=41000
                   WHERE stock_code='600000' AND trade_date='2026-07-28'"""
            )
        with (
            patch.object(sys, "argv", [
                "turnover",
                "--market-db", str(self.source),
                "--as-of", "2026-07-28",
                "--fetched-at", "2026-07-28T17:40:00+08:00",
                "--output", str(self.snapshot),
            ]),
            self.assertRaises(SystemExit) as raised,
        ):
            turnover_cli_main()
        self.assertEqual(2, raised.exception.code)

    def test_derives_turnover_from_bounded_float_share_reference(self) -> None:
        reference = build_float_share_reference_v1(
            market_database_path=self.source,
            capital_database_path=self.capital,
            as_of="2026-07-27",
            created_at="2026-07-28T17:30:00+08:00",
        )
        payload = build_derived_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            reference=reference,
        )

        self.assertEqual("baostock.float-shares-derived", payload["source"])
        self.assertEqual(1, payload["reference_age_sessions"])
        self.assertEqual(
            reference["source_version"],
            payload["reference_source_version"],
        )
        self.assertEqual(64, len(payload["reference_content_sha256"]))
        self.assertEqual(1.0, payload["summary"]["coverage"])
        values = {
            row["stock_code"]: row["turnover_percent"]
            for row in payload["rows"]
        }
        self.assertEqual(0.01, values["000001"])
        self.assertEqual(0.02, values["600000"])

    def test_existing_derived_snapshot_rejects_changed_reference(self) -> None:
        reference = build_float_share_reference_v1(
            market_database_path=self.source,
            capital_database_path=self.capital,
            as_of="2026-07-27",
            created_at="2026-07-28T17:30:00+08:00",
        )
        payload = build_derived_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            reference=reference,
        )
        write_turnover_snapshot_v1(
            payload=payload,
            output=self.snapshot,
        )
        changed = json.loads(json.dumps(reference))
        changed["rows"][0]["float_shares"] *= 2
        reference_path = self.root / "changed-reference.json"
        reference_path.write_text(
            json.dumps(changed),
            encoding="utf-8",
        )
        with (
            patch.object(sys, "argv", [
                "turnover",
                "--market-db", str(self.source),
                "--as-of", "2026-07-28",
                "--fetched-at", "2026-07-28T17:40:00+08:00",
                "--float-share-reference", str(reference_path),
                "--output", str(self.snapshot),
            ]),
            self.assertRaises(SystemExit) as raised,
        ):
            turnover_cli_main()
        self.assertEqual(2, raised.exception.code)

    def test_reference_producer_rejects_unconsumable_source_version(
        self,
    ) -> None:
        with sqlite3.connect(self.capital) as connection:
            connection.execute(
                """UPDATE supplemental_metadata
                   SET value='eastmoney-https-moneyflow+baostock-daily.v3'
                   WHERE key='capital_source_version'"""
            )
        with self.assertRaisesRegex(ValueError, "source version mismatch"):
            build_float_share_reference_v1(
                market_database_path=self.source,
                capital_database_path=self.capital,
                as_of="2026-07-27",
                created_at="2026-07-28T17:30:00+08:00",
            )

    def test_derived_snapshot_rejects_reference_created_after_fetch(
        self,
    ) -> None:
        reference = build_float_share_reference_v1(
            market_database_path=self.source,
            capital_database_path=self.capital,
            as_of="2026-07-27",
            created_at="2026-07-28T17:30:00+08:00",
        )
        with self.assertRaisesRegex(ValueError, "cannot be after fetched_at"):
            build_derived_turnover_snapshot_v1(
                market_database_path=self.source,
                as_of="2026-07-28",
                fetched_at="2026-07-28T17:29:59+08:00",
                reference=reference,
            )

    def test_reference_rejects_capital_fetched_after_creation(self) -> None:
        with sqlite3.connect(self.capital) as connection:
            connection.execute(
                """UPDATE supplemental_metadata
                   SET value='2026-07-28T18:00:00+08:00'
                   WHERE key='capital_fetched_at'"""
            )
        with self.assertRaisesRegex(ValueError, "after created_at"):
            build_float_share_reference_v1(
                market_database_path=self.source,
                capital_database_path=self.capital,
                as_of="2026-07-27",
                created_at="2026-07-28T17:30:00+08:00",
            )

    def test_existing_derived_snapshot_rejects_changed_market_database(
        self,
    ) -> None:
        reference = build_float_share_reference_v1(
            market_database_path=self.source,
            capital_database_path=self.capital,
            as_of="2026-07-27",
            created_at="2026-07-28T17:30:00+08:00",
        )
        payload = build_derived_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            reference=reference,
        )
        write_turnover_snapshot_v1(payload=payload, output=self.snapshot)
        reference_path = self.root / "reference.json"
        reference_path.write_text(json.dumps(reference), encoding="utf-8")
        with sqlite3.connect(self.source) as connection:
            connection.execute(
                """UPDATE stock_daily SET volume=1500
                   WHERE stock_code='000001' AND trade_date='2026-07-28'"""
            )
        with (
            patch.object(sys, "argv", [
                "turnover",
                "--market-db", str(self.source),
                "--as-of", "2026-07-28",
                "--fetched-at", "2026-07-28T17:40:00+08:00",
                "--float-share-reference", str(reference_path),
                "--output", str(self.snapshot),
            ]),
            self.assertRaises(SystemExit) as raised,
        ):
            turnover_cli_main()
        self.assertEqual(2, raised.exception.code)

    def test_rejects_float_share_reference_older_than_twenty_sessions(self) -> None:
        with sqlite3.connect(self.source) as connection:
            connection.executemany(
                """INSERT INTO stock_daily VALUES
                   ('000001','A',?,10,11,9,10,10,900,9000,0,NULL,0)""",
                [(f"2026-06-{day:02d}",) for day in range(1, 22)],
            )
        reference = {
            "schema_version": "baostock-float-share-reference.v1",
            "source": "baostock.daily",
            "source_version": "sina-moneyflow-r0+baostock-daily.v2",
            "as_of": "2026-06-01",
            "created_at": "2026-06-01T18:00:00+08:00",
            "units": {"float_shares": "SHARE"},
            "row_count": 1,
            "rows": [{
                "stock_code": "000001",
                "reference_date": "2026-06-01",
                "float_shares": 10_000_000,
            }],
        }
        with self.assertRaisesRegex(ValueError, "exceeds 20 sessions"):
            build_derived_turnover_snapshot_v1(
                market_database_path=self.source,
                as_of="2026-07-28",
                fetched_at="2026-07-28T17:40:00+08:00",
                reference=reference,
            )

    def test_rejects_unverified_or_internally_inconsistent_reference(self) -> None:
        reference = build_float_share_reference_v1(
            market_database_path=self.source,
            capital_database_path=self.capital,
            as_of="2026-07-27",
            created_at="2026-07-28T17:30:00+08:00",
        )
        cases = (
            ("source", "another.vendor", "source mismatch"),
            ("source_version", "evil-baostock.v1", "source version mismatch"),
            ("row_count", 1, "row count mismatch"),
        )
        for field, value, message in cases:
            with self.subTest(field=field):
                changed = json.loads(json.dumps(reference))
                changed[field] = value
                with self.assertRaisesRegex(ValueError, message):
                    build_derived_turnover_snapshot_v1(
                        market_database_path=self.source,
                        as_of="2026-07-28",
                        fetched_at="2026-07-28T17:40:00+08:00",
                        reference=changed,
                    )

        duplicate = json.loads(json.dumps(reference))
        duplicate["rows"].append(dict(duplicate["rows"][0]))
        duplicate["row_count"] += 1
        with self.assertRaisesRegex(ValueError, "duplicate"):
            build_derived_turnover_snapshot_v1(
                market_database_path=self.source,
                as_of="2026-07-28",
                fetched_at="2026-07-28T17:40:00+08:00",
                reference=duplicate,
            )

    def test_enriches_only_exact_date_after_cross_source_checks(self) -> None:
        self._write_snapshot(_FakeBaoStockClient())

        result = publish_turnover_enriched_daily_market_data(
            source_path=self.source,
            source_health_path=self.health,
            turnover_snapshot_path=self.snapshot,
            target_path=self.target,
            readiness_root=self.readiness,
            as_of="2026-07-28",
            published_at="2026-07-28T17:45:00+08:00",
        )

        self.assertEqual("2026-07-28", result.as_of)
        with sqlite3.connect(self.target) as connection:
            rows = connection.execute(
                "SELECT stock_code,trade_date,turnover FROM stock_daily "
                "ORDER BY trade_date,stock_code"
            ).fetchall()
        self.assertEqual([
            ("000001", "2026-07-27", None),
            ("600000", "2026-07-27", None),
            ("000001", "2026-07-28", 1.25),
            ("600000", "2026-07-28", 2.5),
        ], rows)
        metadata = load_market_metadata(self.target)
        self.assertEqual(
            TURNOVER_ENRICHED_DAILY_VERSION,
            metadata["producer_version"],
        )
        self.assertEqual(
            BAOSTOCK_TURNOVER_SOURCE_VERSION,
            metadata["turnover_source_version"],
        )
        self.assertEqual("PERCENT", metadata["turnover_unit"])
        self.assertEqual("SHARE", metadata["stock_daily_volume_unit"])
        self.assertEqual("CNY", metadata["stock_daily_amount_unit"])
        self.assertEqual("1.0", metadata["turnover_coverage"])

    def test_rejects_lot_share_mismatch_without_replacing_target(self) -> None:
        with sqlite3.connect(self.target) as connection:
            connection.executescript("""
                CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT);
                INSERT INTO stock_daily VALUES ('000001','2026-07-27');
            """)
        original = self.target.read_bytes()
        payload = build_baostock_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            client=_FakeBaoStockClient(),
        )
        payload["rows"][0]["volume"] *= 0.01
        write_turnover_snapshot_v1(
            payload=payload,
            output=self.snapshot,
        )

        with self.assertRaisesRegex(ValueError, "volume mismatch"):
            publish_turnover_enriched_daily_market_data(
                source_path=self.source,
                source_health_path=self.health,
                turnover_snapshot_path=self.snapshot,
                target_path=self.target,
                readiness_root=self.readiness,
                as_of="2026-07-28",
                published_at="2026-07-28T17:45:00+08:00",
            )

        self.assertEqual(original, self.target.read_bytes())
        self.assertFalse(self.readiness.exists())

    def test_rejects_below_frozen_coverage(self) -> None:
        payload = build_baostock_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            client=_FakeBaoStockClient(),
        )
        payload["rows"].pop()
        payload["summary"] = {
            "eligible_row_count": 2,
            "covered_row_count": 1,
            "missing_row_count": 1,
            "coverage": 0.5,
            "missing_stock_codes": ["600000"],
        }
        write_turnover_snapshot_v1(
            payload=payload,
            output=self.snapshot,
        )

        with self.assertRaisesRegex(ValueError, "coverage"):
            publish_turnover_enriched_daily_market_data(
                source_path=self.source,
                source_health_path=self.health,
                turnover_snapshot_path=self.snapshot,
                target_path=self.target,
                readiness_root=self.readiness,
                as_of="2026-07-28",
                published_at="2026-07-28T17:45:00+08:00",
            )

        self.assertFalse(self.target.exists())
        self.assertFalse(self.readiness.exists())

    def test_uncovered_turnover_does_not_retain_legacy_value(self) -> None:
        payload = build_baostock_turnover_snapshot_v1(
            market_database_path=self.source,
            as_of="2026-07-28",
            fetched_at="2026-07-28T17:40:00+08:00",
            client=_FakeBaoStockClient(),
        )
        payload["rows"].pop()
        payload["summary"] = {
            "eligible_row_count": 2,
            "covered_row_count": 1,
            "missing_row_count": 1,
            "coverage": 0.5,
            "missing_stock_codes": ["600000"],
        }
        write_turnover_snapshot_v1(payload=payload, output=self.snapshot)
        with patch(
            "yifei_platform.bootstrap.TURNOVER_COVERAGE_MINIMUM", 0.5
        ):
            self._publish()
        with sqlite3.connect(self.target) as connection:
            turnover = connection.execute(
                """SELECT turnover FROM stock_daily
                   WHERE stock_code='600000' AND trade_date='2026-07-28'"""
            ).fetchone()[0]
        self.assertIsNone(turnover)

    def test_low_coverage_is_not_frozen_as_an_immutable_snapshot(self) -> None:
        with self.assertRaisesRegex(ValueError, "coverage"):
            build_baostock_turnover_snapshot_v1(
                market_database_path=self.source,
                as_of="2026-07-28",
                fetched_at="2026-07-28T17:40:00+08:00",
                client=_FakeBaoStockClient(missing={"600000"}),
            )
        self.assertFalse(self.snapshot.exists())

    def test_rejects_snapshot_fetched_after_publication_time(self) -> None:
        write_turnover_snapshot_v1(
            payload=build_baostock_turnover_snapshot_v1(
                market_database_path=self.source,
                as_of="2026-07-28",
                fetched_at="2026-07-28T17:46:00+08:00",
                client=_FakeBaoStockClient(),
            ),
            output=self.snapshot,
        )
        with self.assertRaisesRegex(ValueError, "after published_at"):
            self._publish()
        self.assertFalse(self.target.exists())
        self.assertFalse(self.readiness.exists())

    def test_rejects_changed_same_day_snapshot(self) -> None:
        self._write_snapshot(_FakeBaoStockClient())
        self._publish()
        payload = json.loads(self.snapshot.read_text(encoding="utf-8"))
        payload["rows"][0]["turnover_percent"] = 1.5
        self.snapshot.unlink()
        write_turnover_snapshot_v1(payload=payload, output=self.snapshot)

        with self.assertRaisesRegex(ValueError, "explicit correction version"):
            self._publish()

    def _write_snapshot(self, client: "_FakeBaoStockClient") -> None:
        write_turnover_snapshot_v1(
            payload=build_baostock_turnover_snapshot_v1(
                market_database_path=self.source,
                as_of="2026-07-28",
                fetched_at="2026-07-28T17:40:00+08:00",
                client=client,
            ),
            output=self.snapshot,
        )

    def _publish(self):
        return publish_turnover_enriched_daily_market_data(
            source_path=self.source,
            source_health_path=self.health,
            turnover_snapshot_path=self.snapshot,
            target_path=self.target,
            readiness_root=self.readiness,
            as_of="2026-07-28",
            published_at="2026-07-28T17:45:00+08:00",
        )


class _FakeBaoStockClient:
    def __init__(
        self,
        *,
        volume_scale: float = 1.0,
        amount_scale: float = 1.0,
        amount_offset: float = 0.0,
        missing: set[str] | None = None,
    ) -> None:
        self._volume_scale = volume_scale
        self._amount_scale = amount_scale
        self._amount_offset = amount_offset
        self._missing = missing or set()

    def read(
        self, stock_code: str, start_date: str, end_date: str
    ) -> tuple[dict[str, object], ...]:
        if stock_code in self._missing:
            return ()
        source = {
            "000001": ("11", "1000", "11000", "1.25"),
            "600000": ("20", "2000", "40000", "2.5"),
        }[stock_code]
        return ({
            "trade_date": start_date,
            "close": source[0],
            "volume": str(float(source[1]) * self._volume_scale),
            "amount": str(
                float(source[2]) * self._amount_scale + self._amount_offset
            ),
            "turnover_percent": source[3],
            "volume_unit": "SHARE",
            "amount_unit": "CNY",
            "turnover_unit": "PERCENT",
        },)


def _login_result(attempts: list[int]):
    attempts.append(1)
    if len(attempts) < 3:
        return types.SimpleNamespace(
            error_code="1",
            error_msg="temporary network error",
        )
    return types.SimpleNamespace(error_code="0", error_msg="")


if __name__ == "__main__":
    unittest.main()
