from __future__ import annotations

import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from yifei_platform.market_data import ReadStatus, StockDailyFactV1
from yifei_platform.public_data_ingestion import (
    AksharePublicDataClientV1,
    SINA_CAPITAL_SOURCE,
    SinaCapitalFlowClientV1,
    backfill_cninfo_membership_v1,
    backfill_public_capital_v1,
    backfill_public_supplemental_v1,
    derive_float_market_cap_cny_v1,
    prepare_public_cache_v1,
    parse_eastmoney_capital_payload_v1,
    parse_sina_capital_payload_v1,
    prefetch_public_capital_v1,
    validate_vendor_flow_against_turnover_cny_v1,
)
from yifei_platform.supplemental_facts import (
    SectorMembershipReaderV1,
    StockCapitalFactReaderV1,
    calculate_sector_strength_v1,
    initialize_supplemental_database_v1,
    migrate_legacy_board_facts_v1,
    publish_supplemental_readiness_v1,
)
from yifei_platform.tushare_ingestion import backfill_tushare_supplemental_v1


class SupplementalFactsContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name)
        self.db_path = self.root / "supplemental.db"
        self._seed_supplemental(self.db_path)

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_stock_capital_reader_preserves_vendor_semantics_and_units(self) -> None:
        result = StockCapitalFactReaderV1(self.db_path).read_daily("2026-07-09")
        self.assertEqual(ReadStatus.OK, result.status)
        fact = result.facts[0]
        self.assertEqual("000001", fact.stock_code)
        self.assertEqual(120.0, fact.vendor_net_amount)
        self.assertEqual(24000.0, fact.float_market_cap)
        self.assertEqual("CNY_10K", fact.amount_unit)
        self.assertEqual("tushare.moneyflow_ths+daily_basic", fact.source)
        self.assertAlmostEqual(0.005, fact.net_inflow_ratio)
        self.assertFalse(hasattr(fact, "institutional_inflow"))

    def test_membership_reader_is_point_in_time_and_rejects_ambiguity(self) -> None:
        reader = SectorMembershipReaderV1(self.db_path)
        old = reader.read_as_of("2026-06-30")
        current = reader.read_as_of("2026-07-09")
        self.assertEqual("801010.SI", old.facts[0].sector_code)
        self.assertEqual("801020.SI", current.facts[0].sector_code)

        with sqlite3.connect(self.db_path) as connection:
            connection.execute(
                """INSERT INTO sector_membership_history
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "000001", "平安银行", "801030.SI", "化工", "L2",
                    "2026-07-01", None, "tushare.index_member_all",
                    "sw2021.v1", "2026-07-27T10:00:00+08:00",
                ),
            )
        blocked = reader.read_as_of("2026-07-09")
        self.assertEqual(ReadStatus.BLOCKED, blocked.status)
        self.assertIn("ambiguous_membership:000001:L2", blocked.reason_codes)

    def test_optional_name_columns_are_not_required_by_readers(self) -> None:
        minimal = self.root / "minimal.db"
        with sqlite3.connect(minimal) as connection:
            connection.executescript("""
                CREATE TABLE stock_capital_daily (
                    stock_code TEXT, trade_date TEXT,
                    vendor_net_amount REAL, float_market_cap REAL,
                    amount_unit TEXT, market_cap_unit TEXT, source TEXT,
                    source_version TEXT, fetched_at TEXT
                );
                INSERT INTO stock_capital_daily VALUES
                    ('000001','2026-07-09',1,100,'CNY','CNY',
                     'vendor','vendor.v1','2026-07-09T18:00:00+08:00');
                CREATE TABLE sector_membership_history (
                    stock_code TEXT, sector_code TEXT, sector_level TEXT,
                    valid_from TEXT, valid_to_exclusive TEXT, source TEXT,
                    source_version TEXT, fetched_at TEXT
                );
                INSERT INTO sector_membership_history VALUES
                    ('000001','S1','L2','2026-07-09',NULL,
                     'vendor','vendor.v1','2026-07-09T18:00:00+08:00');
            """)

        capital = StockCapitalFactReaderV1(minimal).read_daily("2026-07-09")
        membership = SectorMembershipReaderV1(minimal).read_as_of("2026-07-09")

        self.assertEqual(ReadStatus.OK, capital.status)
        self.assertIsNone(capital.facts[0].stock_name)
        self.assertEqual(ReadStatus.OK, membership.status)
        self.assertIsNone(membership.facts[0].stock_name)
        self.assertIsNone(membership.facts[0].sector_name)

    def test_sector_strength_uses_only_active_members_and_raw_returns(self) -> None:
        memberships = SectorMembershipReaderV1(self.db_path).read_as_of(
            "2026-07-09"
        ).facts
        bars = (
            self._bar("000001", 2.0),
            self._bar("000002", -1.0),
            self._bar("000003", 3.0),
        )
        result = calculate_sector_strength_v1(
            as_of="2026-07-09",
            memberships=memberships,
            stock_daily=bars,
        )
        self.assertEqual(1, len(result))
        fact = result[0]
        self.assertEqual("801020.SI", fact.sector_code)
        self.assertEqual(3, fact.member_count)
        self.assertEqual(3, fact.observed_member_count)
        self.assertEqual(2, fact.advancing_count)
        self.assertAlmostEqual(2 / 3, fact.advancing_ratio)
        self.assertEqual(2.0, fact.median_pct_chg)
        self.assertFalse(hasattr(fact, "score"))

    def test_legacy_board_migration_is_atomic_and_does_not_copy_scores(self) -> None:
        legacy = self.root / "legacy.db"
        target = self.root / "facts.db"
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                """CREATE TABLE ths_board_daily (
                    board_code TEXT, board_name TEXT, trade_date TEXT,
                    open REAL, high REAL, low REAL, close REAL,
                    volume REAL, amount REAL, pct_chg REAL,
                    PRIMARY KEY(board_code, trade_date))"""
            )
            connection.execute(
                "INSERT INTO ths_board_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
                ("B001", "机器人", "2026-07-09", 1, 2, 1, 2, 3, 4, 5),
            )
            connection.execute(
                "CREATE TABLE sector_health_daily (board_code TEXT, score REAL)"
            )
        result = migrate_legacy_board_facts_v1(
            source_path=legacy,
            target_path=target,
            published_at="2026-07-27T10:00:00+08:00",
            source_version="legacy-ths-board.2026-07-27.v1",
        )
        self.assertEqual("2026-07-09", result.latest_available_as_of)
        with sqlite3.connect(target) as connection:
            tables = {
                row[0] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
        self.assertIn("ths_board_daily", tables)
        self.assertNotIn("sector_health_daily", tables)
        before = target.read_bytes()
        migrate_legacy_board_facts_v1(
            source_path=legacy,
            target_path=target,
            published_at="2026-07-27T10:00:00+08:00",
            source_version="legacy-ths-board.2026-07-27.v1",
        )
        self.assertEqual(before, target.read_bytes())
        with sqlite3.connect(legacy) as connection:
            connection.execute(
                """UPDATE ths_board_daily SET close=3
                   WHERE board_code='B001' AND trade_date='2026-07-09'"""
            )
        with self.assertRaisesRegex(FileExistsError, "content differs"):
            migrate_legacy_board_facts_v1(
                source_path=legacy,
                target_path=target,
                published_at="2026-07-27T10:00:00+08:00",
                source_version="legacy-ths-board.2026-07-27.v1",
            )
        self.assertEqual(before, target.read_bytes())

    def test_tushare_backfill_joins_same_source_units_and_pit_membership(self) -> None:
        market = self.root / "market.db"
        target = self.root / "backfill.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, ?)",
                [
                    (code, "2026-07-09")
                    for code in ("000001", "000002", "000003")
                ],
            )
        result = backfill_tushare_supplemental_v1(
            client=_FakeTushareClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-27T10:00:00+08:00",
        )
        self.assertEqual("2026-07-09", result.latest_capital_as_of)
        capital = StockCapitalFactReaderV1(target).read_daily("2026-07-09")
        memberships = SectorMembershipReaderV1(target).read_as_of("2026-07-09")
        self.assertEqual(ReadStatus.OK, capital.status)
        self.assertEqual(3, len(capital.facts))
        self.assertEqual(ReadStatus.OK, memberships.status)
        self.assertEqual(3, len(memberships.facts))
        self.assertEqual(
            ReadStatus.MISSING,
            SectorMembershipReaderV1(target).read_as_of(
                "2026-07-08"
            ).status,
        )
        self.assertEqual(
            ReadStatus.MISSING,
            SectorMembershipReaderV1(target).read_as_of(
                "2026-07-10"
            ).status,
        )
        with sqlite3.connect(target) as connection:
            metadata = dict(connection.execute(
                "SELECT key,value FROM supplemental_metadata"
            ))
        self.assertEqual(
            "2026-07-09", metadata["membership_available_from"]
        )
        self.assertEqual(
            "2026-07-09", metadata["membership_available_through"]
        )

    def test_tushare_smaller_rerun_preserves_validated_outer_history(self) -> None:
        market = self.root / "tushare-rerun-market.db"
        target = self.root / "tushare-rerun.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, ?)",
                [
                    (code, session)
                    for session in (
                        "2026-07-08",
                        "2026-07-09",
                        "2026-07-10",
                    )
                    for code in ("000001", "000002", "000003")
                ],
            )
        backfill_tushare_supplemental_v1(
            client=_FakeTushareClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-08",
            end_date="2026-07-10",
            fetched_at="2026-07-27T10:00:00+08:00",
        )
        backfill_tushare_supplemental_v1(
            client=_FakeTushareClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-28T10:00:00+08:00",
        )

        reader = SectorMembershipReaderV1(target)
        for session in ("2026-07-08", "2026-07-09", "2026-07-10"):
            with self.subTest(session=session):
                self.assertEqual(
                    ReadStatus.OK,
                    reader.read_as_of(session).status,
                )
        with sqlite3.connect(target) as connection:
            metadata = dict(connection.execute(
                """SELECT key,value FROM supplemental_metadata
                   WHERE key LIKE 'membership_available_%'"""
            ))
        self.assertEqual(
            "2026-07-08", metadata["membership_available_from"]
        )
        self.assertEqual(
            "2026-07-10", metadata["membership_available_through"]
        )

    def test_tushare_backfill_preserves_rows_owned_by_other_sources(
        self,
    ) -> None:
        market = self.root / "tushare-shared-market.db"
        target = self.root / "tushare-shared.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, '2026-07-09')",
                [(code,) for code in ("000001", "000002", "000003")],
            )
        backfill_tushare_supplemental_v1(
            client=_FakeTushareClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-27T10:00:00+08:00",
        )
        with sqlite3.connect(target) as connection:
            connection.execute(
                """INSERT INTO stock_capital_daily VALUES
                   (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "999999", None, "2026-07-09", 1, 2, "CNY", "CNY",
                    "another.provider", "another.v1",
                    "2026-07-27T10:00:00+08:00",
                ),
            )
        backfill_tushare_supplemental_v1(
            client=_FakeTushareClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-28T10:00:00+08:00",
        )
        with sqlite3.connect(target) as connection:
            preserved = connection.execute(
                """SELECT source FROM stock_capital_daily
                   WHERE stock_code='999999' AND trade_date='2026-07-09'"""
            ).fetchone()
        self.assertEqual(("another.provider",), preserved)

    def test_tushare_backfill_fails_closed_on_cross_source_key_conflict(
        self,
    ) -> None:
        market = self.root / "tushare-conflict-market.db"
        target = self.root / "tushare-conflict.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, '2026-07-09')",
                [(code,) for code in ("000001", "000002", "000003")],
            )
        kwargs = {
            "client": _FakeTushareClient(),
            "market_database_path": market,
            "target_path": target,
            "start_date": "2026-07-09",
            "end_date": "2026-07-09",
        }
        backfill_tushare_supplemental_v1(
            **kwargs,
            fetched_at="2026-07-27T10:00:00+08:00",
        )
        with sqlite3.connect(target) as connection:
            connection.execute(
                """UPDATE stock_capital_daily SET source='another.provider'
                   WHERE stock_code='000001' AND trade_date='2026-07-09'"""
            )
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "cross-source"):
            backfill_tushare_supplemental_v1(
                **kwargs,
                fetched_at="2026-07-28T10:00:00+08:00",
            )
        self.assertEqual(before, target.read_bytes())

    def test_public_backfill_normalizes_to_shares_cny_and_pit_sw_l2(self) -> None:
        market = self.root / "public-market.db"
        target = self.root / "public-backfill.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                """CREATE TABLE stock_daily (
                    stock_code TEXT, stock_name TEXT, trade_date TEXT
                )"""
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, ?, ?)",
                [
                    (code, code, "2026-07-09")
                    for code in ("000001", "000002", "000003")
                ],
            )
        result = backfill_public_supplemental_v1(
            capital_client=_FakePublicCapitalClient(),
            daily_client=_FakeBaoStockClient(),
            industry_client=_FakeCninfoClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-27T10:00:00+08:00",
        )
        self.assertEqual("2026-07-09", result.latest_capital_as_of)
        capital = StockCapitalFactReaderV1(target).read_daily("2026-07-09")
        membership = SectorMembershipReaderV1(target).read_as_of("2026-07-09")
        self.assertEqual(ReadStatus.OK, capital.status)
        self.assertEqual("CNY", capital.facts[0].amount_unit)
        self.assertEqual(2_000_000_000.0, capital.facts[0].float_market_cap)
        self.assertAlmostEqual(0.00005, capital.facts[0].net_inflow_ratio)
        self.assertEqual(ReadStatus.OK, membership.status)
        self.assertEqual("CNINFO_SW_L2:银行", membership.facts[0].sector_code)
        with sqlite3.connect(target) as connection:
            metadata = dict(connection.execute(
                "SELECT key, value FROM supplemental_metadata"
            ))
        self.assertEqual("SHARE", metadata["capital_volume_raw_unit"])
        self.assertEqual("PERCENT", metadata["capital_turnover_raw_unit"])

    def test_public_backfill_preserves_rows_owned_by_other_sources(
        self,
    ) -> None:
        market = self.root / "public-shared-market.db"
        target = self.root / "public-shared.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                """CREATE TABLE stock_daily (
                    stock_code TEXT, stock_name TEXT, trade_date TEXT
                )"""
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, ?, '2026-07-09')",
                [(code, code) for code in ("000001", "000002", "000003")],
            )
        kwargs = {
            "capital_client": _FakePublicCapitalClient(),
            "daily_client": _FakeBaoStockClient(),
            "industry_client": _FakeCninfoClient(),
            "market_database_path": market,
            "target_path": target,
            "start_date": "2026-07-09",
            "end_date": "2026-07-09",
        }
        backfill_public_supplemental_v1(
            **kwargs,
            fetched_at="2026-07-27T10:00:00+08:00",
        )
        with sqlite3.connect(target) as connection:
            connection.execute(
                """INSERT INTO stock_capital_daily VALUES
                   (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "999999", None, "2026-07-09", 1, 2, "CNY", "CNY",
                    "another.provider", "another.v1",
                    "2026-07-27T10:00:00+08:00",
                ),
            )
            connection.execute(
                """INSERT INTO sector_membership_history VALUES
                   (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "999999", None, "OTHER", "其他", "L2",
                    "2020-01-01", None, "another.provider", "another.v1",
                    "2026-07-27T10:00:00+08:00",
                ),
            )
        backfill_public_supplemental_v1(
            **kwargs,
            fetched_at="2026-07-28T10:00:00+08:00",
        )
        with sqlite3.connect(target) as connection:
            capital_source = connection.execute(
                """SELECT source FROM stock_capital_daily
                   WHERE stock_code='999999'"""
            ).fetchone()
            membership_source = connection.execute(
                """SELECT source FROM sector_membership_history
                   WHERE stock_code='999999'"""
            ).fetchone()
        self.assertEqual(("another.provider",), capital_source)
        self.assertEqual(("another.provider",), membership_source)

    def test_eastmoney_payload_and_turnover_units_are_cross_checked(self) -> None:
        payload = {
            "rc": 0,
            "data": {
                "klines": [
                    "2026-07-09,100000,0,0,0,0,0.1,0,0,0,0,10,1,0,0"
                ]
            },
        }
        rows = parse_eastmoney_capital_payload_v1(payload)
        self.assertEqual(100_000, rows[0]["vendor_net_amount"])
        self.assertEqual(0.1, rows[0]["vendor_net_ratio_percent"])

        market = self.root / "capital-market.db"
        target = self.root / "capital-only.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, ?)",
                [
                    (code, "2026-07-09")
                    for code in ("000001", "000002", "000003")
                ],
            )
        self._seed_supplemental(target)
        with sqlite3.connect(target) as connection:
            connection.execute("DELETE FROM stock_capital_daily")
            connection.execute(
                """INSERT INTO stock_capital_daily VALUES
                   (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "999999", None, "2026-07-09", 1, 2, "CNY", "CNY",
                    "another.provider", "another.v1",
                    "2026-07-27T10:00:00+08:00",
                ),
            )
        result = backfill_public_capital_v1(
            capital_client=_FakePublicCapitalClient(),
            daily_client=_FakeBaoStockClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-28T12:00:00+08:00",
        )
        self.assertEqual(1.0, result.coverage)
        self.assertEqual(
            3,
            len([
                fact for fact in StockCapitalFactReaderV1(target).read_daily(
                    "2026-07-09"
                ).facts
                if fact.source != "another.provider"
            ]),
        )
        self.assertEqual(
            ReadStatus.OK,
            SectorMembershipReaderV1(target).read_as_of(
                "2026-07-09"
            ).status,
        )
        with sqlite3.connect(target) as connection:
            preserved = connection.execute(
                """SELECT source FROM stock_capital_daily
                   WHERE stock_code='999999'"""
            ).fetchone()
        self.assertEqual(("another.provider",), preserved)

    def test_sina_payload_uses_r0_as_main_flow_and_preserves_total_flow(self) -> None:
        rows = parse_sina_capital_payload_v1([{
            "opendate": "2026-07-09",
            "netamount": "250000",
            "ratioamount": "0.0025",
            "r0_net": "100000",
            "r0_ratio": "0.001",
            "turnover": "12.3",
        }])

        self.assertEqual(100_000, rows[0]["vendor_net_amount"])
        self.assertEqual(0.1, rows[0]["vendor_net_ratio_percent"])
        self.assertEqual(250_000, rows[0]["vendor_total_net_amount"])
        self.assertEqual(0.25, rows[0]["vendor_total_net_ratio_percent"])
        self.assertEqual(
            "SINA_SINGLE_TRADE_GTE_CNY_1M",
            rows[0]["vendor_flow_definition"],
        )
        self.assertEqual("VALID", rows[0]["vendor_row_status"])
        self.assertEqual("SINA_LEGACY_UNVERIFIED", rows[0]["turnover_raw_unit"])

    def test_sina_zero_flow_allows_missing_ratio_but_nonzero_does_not(self) -> None:
        zero = {
            "opendate": "2026-02-10",
            "netamount": "0",
            "ratioamount": "0",
            "r0_net": "0",
            "r0_ratio": None,
            "turnover": "0",
        }
        rows = parse_sina_capital_payload_v1([zero])
        self.assertEqual(0, rows[0]["vendor_net_ratio_percent"])

        invalid = dict(zero, r0_net="1")
        with self.assertRaisesRegex(ValueError, "is missing"):
            parse_sina_capital_payload_v1([invalid])

    def test_sina_impossible_vendor_ratio_is_retained_but_not_publishable(self) -> None:
        rows = parse_sina_capital_payload_v1([{
            "opendate": "2026-06-09",
            "netamount": "107807106.81",
            "ratioamount": "1.27971",
            "r0_net": "107125976.56",
            "r0_ratio": "1.27162216",
            "turnover": "407.709",
        }])

        self.assertEqual(
            "INVALID_VENDOR_RATIO",
            rows[0]["vendor_row_status"],
        )
        self.assertGreater(rows[0]["vendor_net_ratio_percent"], 100)

    def test_sina_cross_source_mismatch_is_coverage_miss_not_unit_relaxation(self) -> None:
        flow = {
            "vendor_net_amount": -717066,
            "vendor_net_ratio_percent": -6.768511,
            "turnover_consistency_min": 0.75,
            "turnover_consistency_max": 1.25,
            "turnover_mismatch_policy": "EXCLUDE_ROW",
        }
        daily = {"amount": 22_583_812.86}

        self.assertFalse(validate_vendor_flow_against_turnover_cny_v1(
            stock_code="000632",
            flow=flow,
            daily=daily,
            mismatch_policy="EXCLUDE_ROW",
        ))

    def test_sina_client_uses_exchange_prefix_and_immutable_cache(self) -> None:
        calls = []

        def reader(*, stock_code, symbol, days):
            calls.append((stock_code, symbol, days))
            return [{
                "opendate": "2026-07-09",
                "netamount": "250000",
                "ratioamount": "0.0025",
                "r0_net": "100000",
                "r0_ratio": "0.001",
                "turnover": "12.3",
            }]

        client = SinaCapitalFlowClientV1(
            cache_dir=self.root / "sina-cache",
            request_interval_seconds=0,
            fund_flow_payload_reader=reader,
        )
        first = client.read("600000")
        first[0]["vendor_net_amount"] = 0
        second = client.read("600000")

        self.assertEqual(100_000, second[0]["vendor_net_amount"])
        self.assertEqual([("600000", "sh600000", 120)], calls)

    def test_capital_writer_records_sina_source_without_mixing_vendors(self) -> None:
        market = self.root / "sina-market.db"
        target = self.root / "sina-capital.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, '2026-07-09')",
                [(code,) for code in ("000001", "000002", "000003")],
            )

        backfill_public_capital_v1(
            capital_client=_FakePublicCapitalClient(),
            daily_client=_FakeBaoStockClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-28T12:00:00+08:00",
            source_version="sina-moneyflow-r0+baostock-daily.v2",
            capital_source=SINA_CAPITAL_SOURCE,
        )

        with sqlite3.connect(target) as connection:
            sources = {
                row[0] for row in connection.execute(
                    "SELECT DISTINCT source FROM stock_capital_daily"
                )
            }
            metadata = dict(connection.execute(
                "SELECT key, value FROM supplemental_metadata"
            ))
        self.assertEqual({SINA_CAPITAL_SOURCE}, sources)
        self.assertEqual(SINA_CAPITAL_SOURCE, metadata["capital_source"])

    def test_vendor_flow_unit_mismatch_blocks_capital_publication(self) -> None:
        market = self.root / "capital-unit-market.db"
        target = self.root / "capital-unit.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.execute(
                "INSERT INTO stock_daily VALUES ('000001', '2026-07-09')"
            )
        with self.assertRaisesRegex(ValueError, "ratio/turnover unit mismatch"):
            backfill_public_capital_v1(
                capital_client=_FakeBadRatioCapitalClient(),
                daily_client=_FakeBaoStockClient(),
                market_database_path=market,
                target_path=target,
                start_date="2026-07-09",
                end_date="2026-07-09",
                fetched_at="2026-07-28T12:00:00+08:00",
            )
        self.assertFalse(target.exists())

    def test_suspended_daily_row_is_missing_not_zero_or_batch_error(self) -> None:
        market = self.root / "suspended-market.db"
        target = self.root / "suspended-capital.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.execute(
                "INSERT INTO stock_daily VALUES ('000001', '2026-07-09')"
            )

        result = backfill_public_capital_v1(
            capital_client=_FakePublicCapitalClient(),
            daily_client=_FakeSuspendedBaoStockClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-28T12:00:00+08:00",
            minimum_capital_coverage=0,
        )

        self.assertEqual(0, result.coverage)
        self.assertEqual(
            ReadStatus.MISSING,
            StockCapitalFactReaderV1(target).read_daily(
                "2026-07-09"
            ).status,
        )

    def test_latest_capital_session_must_meet_frozen_coverage(self) -> None:
        market = self.root / "latest-coverage-market.db"
        target = self.root / "latest-coverage.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, ?)",
                [
                    (code, session)
                    for session in ("2026-07-09", "2026-07-10")
                    for code in ("000001", "000002", "000003")
                ],
            )

        class FirstDayCapital:
            def read(self, _stock_code):
                return ({
                    "trade_date": "2026-07-09",
                    "vendor_net_amount": 100_000,
                    "vendor_net_ratio_percent": 0.1,
                    "amount_unit": "CNY",
                },)

        class TwoDayDaily:
            def read(self, _stock_code, _start_date, _end_date):
                return tuple({
                    "trade_date": session,
                    "close": 10,
                    "volume": 10_000_000,
                    "amount": 100_000_000,
                    "turnover_percent": 5,
                    "volume_unit": "SHARE",
                    "amount_unit": "CNY",
                    "turnover_unit": "PERCENT",
                } for session in ("2026-07-09", "2026-07-10"))

        with self.assertRaisesRegex(ValueError, "latest session"):
            backfill_public_capital_v1(
                capital_client=FirstDayCapital(),
                daily_client=TwoDayDaily(),
                market_database_path=market,
                target_path=target,
                start_date="2026-07-09",
                end_date="2026-07-10",
                fetched_at="2026-07-28T12:00:00+08:00",
                minimum_capital_coverage=0.5,
            )
        self.assertFalse(target.exists())

    def test_membership_only_backfill_preserves_existing_capital(self) -> None:
        market = self.root / "membership-market.db"
        target = self.root / "membership-only.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, ?)",
                [
                    (code, "2026-07-09")
                    for code in ("000001", "000002", "000003")
                ],
            )
        self._seed_supplemental(target)
        with sqlite3.connect(target) as connection:
            before = connection.execute(
                "SELECT * FROM stock_capital_daily"
            ).fetchall()

        result = backfill_cninfo_membership_v1(
            industry_client=_FakeCninfoClient(),
            market_database_path=market,
            target_path=target,
            start_date="2026-07-09",
            end_date="2026-07-09",
            fetched_at="2026-07-27T10:00:00+08:00",
        )

        self.assertEqual(3, result.stock_count)
        self.assertEqual(1.0, result.coverage)
        membership = SectorMembershipReaderV1(target).read_as_of("2026-07-09")
        self.assertEqual(ReadStatus.BLOCKED, membership.status)
        self.assertTrue(any(
            reason.startswith("ambiguous_membership:")
            for reason in membership.reason_codes
        ))
        with sqlite3.connect(target) as connection:
            after = connection.execute(
                "SELECT * FROM stock_capital_daily"
            ).fetchall()
            membership_sources = {
                row[0] for row in connection.execute(
                    "SELECT DISTINCT source FROM sector_membership_history"
                )
            }
        self.assertEqual(before, after)
        self.assertEqual(
            {"tushare.index_member_all", "akshare.cninfo"},
            membership_sources,
        )

    def test_cninfo_empty_records_are_cached_as_empty_after_retries(self) -> None:
        class EmptyCninfo:
            calls = 0

            def stock_industry_change_cninfo(self, **_kwargs):
                self.calls += 1
                raise KeyError("变更日期")

        cache = self.root / "empty-cninfo-cache"
        client = AksharePublicDataClientV1.__new__(
            AksharePublicDataClientV1
        )
        client._ak = EmptyCninfo()
        client._cache_dir = cache
        client._retry_attempts = 2

        self.assertEqual(
            (),
            client.read_industry("000004", "1990-01-01", "2026-07-09"),
        )
        self.assertEqual(2, client._ak.calls)
        self.assertEqual(
            (),
            client.read_industry("000004", "1990-01-01", "2026-07-09"),
        )
        self.assertEqual(2, client._ak.calls)

    def test_single_stock_transport_failure_requires_healthy_control(self) -> None:
        calls = []

        def reader(*, stock_code, market_number):
            calls.append((stock_code, market_number))
            if stock_code == "000061":
                raise ConnectionError("empty response")
            return {
                "rc": 0,
                "data": {
                    "klines": [
                        "2026-07-09,100000,0,0,0,0,0.1,0,0,0,0,10,1,0,0"
                    ]
                },
            }

        client = AksharePublicDataClientV1.__new__(
            AksharePublicDataClientV1
        )
        client._ak = object()
        client._cache_dir = self.root / "controlled-missing-cache"
        client._retry_attempts = 2
        client._fund_flow_payload_reader = reader

        self.assertEqual((), client.read("000061"))
        self.assertEqual(
            [("000061", "0"), ("000061", "0"), ("000001", "0")],
            calls,
        )
        self.assertFalse(client.has_capital_cache("000061"))
        self.assertEqual((), client.read("000061"))
        self.assertEqual(6, len(calls))

    def test_capital_prefetch_is_bounded_and_resumes_cached_codes(self) -> None:
        market = self.root / "prefetch-market.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES (?, '2026-07-09')",
                [(code,) for code in ("000001", "000002", "000003")],
            )
        client = _FakeCapitalPrefetchClient({"000001"})

        result = prefetch_public_capital_v1(
            capital_client=client,
            market_database_path=market,
            start_date="2026-07-09",
            end_date="2026-07-09",
            batch_size=1,
        )

        self.assertEqual(1, result.cached_before)
        self.assertEqual(1, result.prefetched_count)
        self.assertEqual(1, result.remaining_count)
        self.assertEqual(["000002"], client.read_codes)

    def test_share_lot_mismatch_blocks_before_market_cap_publication(self) -> None:
        valid = {
            "close": 10,
            "volume": 10_000_000,
            "amount": 100_000_000,
            "turnover_percent": 5,
            "volume_unit": "SHARE",
            "amount_unit": "CNY",
            "turnover_unit": "PERCENT",
        }
        self.assertEqual(
            2_000_000_000,
            derive_float_market_cap_cny_v1(
                stock_code="000001", row=valid
            ),
        )
        lots_mislabeled_as_shares = dict(valid)
        lots_mislabeled_as_shares["volume"] = 100_000
        with self.assertRaisesRegex(ValueError, "unit mismatch"):
            derive_float_market_cap_cny_v1(
                stock_code="000001", row=lots_mislabeled_as_shares
            )

    def test_share_lot_mismatch_cannot_replace_existing_database(self) -> None:
        market = self.root / "unit-market.db"
        target = self.root / "existing.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.execute(
                "INSERT INTO stock_daily VALUES ('000001', '2026-07-09')"
            )
        target.write_bytes(b"existing-published-database")
        before = target.read_bytes()
        with self.assertRaisesRegex(ValueError, "unit mismatch"):
            backfill_public_supplemental_v1(
                capital_client=_FakePublicCapitalClient(),
                daily_client=_FakeLotsMislabeledAsSharesClient(),
                industry_client=_FakeCninfoClient(),
                market_database_path=market,
                target_path=target,
                start_date="2026-07-09",
                end_date="2026-07-09",
                fetched_at="2026-07-27T10:00:00+08:00",
            )
        self.assertEqual(before, target.read_bytes())

    def test_raw_unit_declarations_are_mandatory(self) -> None:
        row = {
            "close": 10,
            "volume": 10_000_000,
            "amount": 100_000_000,
            "turnover_percent": 5,
            "volume_unit": "LOT",
            "amount_unit": "CNY",
            "turnover_unit": "PERCENT",
        }
        with self.assertRaisesRegex(ValueError, "must be SHARE"):
            derive_float_market_cap_cny_v1(stock_code="000001", row=row)

    def test_public_cache_cannot_be_reused_for_another_range(self) -> None:
        cache = self.root / "public-cache"
        prepare_public_cache_v1(
            cache_dir=cache,
            start_date="2026-04-01",
            end_date="2026-07-09",
        )
        prepare_public_cache_v1(
            cache_dir=cache,
            start_date="2026-04-01",
            end_date="2026-07-09",
        )
        with self.assertRaisesRegex(ValueError, "different range"):
            prepare_public_cache_v1(
                cache_dir=cache,
                start_date="2026-04-01",
                end_date="2026-07-10",
            )

    def test_supplemental_readiness_discloses_status_without_holdout_counts(self) -> None:
        marker = publish_supplemental_readiness_v1(
            database_path=self.db_path,
            readiness_root=self.root / "readiness",
            as_of="2026-07-09",
            published_at="2026-07-27T10:00:00+08:00",
            source_versions={
                "stock_capital_daily": "tushare.2026-07.v1",
                "sector_membership_history": "sw2021.v1",
            },
            dataset_coverages={
                "stock_capital_daily": None,
                "sector_membership_history": None,
            },
            dataset_gate_coverages={
                "stock_capital_daily": 1.0,
            },
        )
        self.assertEqual("ready", marker.status)
        snapshot = json.loads(next(
            (self.root / "readiness" / "quality").rglob("*.json")
        ).read_text(encoding="utf-8"))
        self.assertTrue(all(
            dataset["coverage"] is None
            for dataset in snapshot["datasets"]
        ))
        self.assertNotIn("row_count", snapshot)

    def test_readiness_cannot_claim_missing_or_wrong_source_dataset(self) -> None:
        for as_of, source_version in (
            ("2026-07-08", "tushare.2026-07.v1"),
            ("2026-07-09", "wrong-source.v1"),
        ):
            with self.subTest(as_of=as_of, source_version=source_version):
                with self.assertRaisesRegex(ValueError, "missing dataset"):
                    publish_supplemental_readiness_v1(
                        database_path=self.db_path,
                        readiness_root=self.root / "blocked-readiness",
                        as_of=as_of,
                        published_at="2026-07-27T10:00:00+08:00",
                        source_versions={
                            "stock_capital_daily": source_version,
                        },
                        dataset_coverages={
                            "stock_capital_daily": None,
                        },
                        dataset_gate_coverages={
                            "stock_capital_daily": 1.0,
                        },
                    )

    def test_capital_readiness_requires_frozen_coverage_proof(self) -> None:
        with self.assertRaisesRegex(ValueError, "coverage"):
            publish_supplemental_readiness_v1(
                database_path=self.db_path,
                readiness_root=self.root / "low-capital-coverage",
                as_of="2026-07-09",
                published_at="2026-07-27T10:00:00+08:00",
                source_versions={
                    "stock_capital_daily": "tushare.2026-07.v1",
                },
                dataset_coverages={
                    "stock_capital_daily": None,
                },
                dataset_gate_coverages={
                    "stock_capital_daily": 0.979,
                },
            )

    def test_sector_flow_readiness_rechecks_frozen_completeness_floor(
        self,
    ) -> None:
        initialize_supplemental_database_v1(self.db_path)
        with sqlite3.connect(self.db_path) as connection:
            connection.executemany(
                """INSERT INTO sector_fund_flow_daily (
                       trade_date, sector_code, sector_name, amount,
                       change_pct, main_inflow, up_count, down_count,
                       lead_stock_name, lead_stock_chg, amount_unit,
                       main_inflow_unit, source, source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        "2026-07-09", f"BK{index:04d}", None, None,
                        None, 1.0, None, None, None, None, "CNY",
                        "CNY", "akshare.sector_em.industry",
                        "sector-em.v1", "2026-07-27T15:20:00+08:00",
                    )
                    for index in range(399)
                ),
            )
        with self.assertRaisesRegex(ValueError, "incomplete dataset"):
            publish_supplemental_readiness_v1(
                database_path=self.db_path,
                readiness_root=self.root / "incomplete-sector-readiness",
                as_of="2026-07-09",
                published_at="2026-07-27T15:30:00+08:00",
                source_versions={
                    "sector_fund_flow_daily": "sector-em.v1",
                },
                dataset_coverages={
                    "sector_fund_flow_daily": None,
                },
            )

    def test_readiness_requires_l2_membership_and_cny_sector_units(
        self,
    ) -> None:
        database = self.root / "readiness-contract.db"
        initialize_supplemental_database_v1(database)
        with sqlite3.connect(database) as connection:
            connection.execute(
                """INSERT INTO sector_membership_history VALUES
                   (?,?,?,?,?,?,?,?,?,?)""",
                (
                    "000001", None, "L1", "一级", "L1",
                    "2020-01-01", None, "vendor", "membership.v1",
                    "2026-07-27T15:20:00+08:00",
                ),
            )
            connection.executemany(
                """INSERT INTO sector_fund_flow_daily (
                       trade_date, sector_code, sector_name, amount,
                       change_pct, main_inflow, up_count, down_count,
                       lead_stock_name, lead_stock_chg, amount_unit,
                       main_inflow_unit, source, source_version, fetched_at
                   ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    (
                        "2026-07-09", f"BK{index:04d}", None, None,
                        None, 1.0, None, None, None, None,
                        "CNY" if index else "CNY_10K",
                        "CNY", "vendor", "sector.v1",
                        "2026-07-27T15:20:00+08:00",
                    )
                    for index in range(400)
                ),
            )
        for dataset, source_version in (
            ("sector_membership_history", "membership.v1"),
            ("sector_fund_flow_daily", "sector.v1"),
        ):
            with self.subTest(dataset=dataset):
                with self.assertRaisesRegex(
                    ValueError, "missing dataset|incomplete dataset"
                ):
                    publish_supplemental_readiness_v1(
                        database_path=database,
                        readiness_root=self.root / f"blocked-{dataset}",
                        as_of="2026-07-09",
                        published_at="2026-07-27T15:30:00+08:00",
                        source_versions={dataset: source_version},
                        dataset_coverages={dataset: None},
                    )

    def test_capital_source_history_window_is_fail_closed(self) -> None:
        market = self.root / "window-market.db"
        target = self.root / "window-target.db"
        with sqlite3.connect(market) as connection:
            connection.execute(
                "CREATE TABLE stock_daily (stock_code TEXT, trade_date TEXT)"
            )
            connection.executemany(
                "INSERT INTO stock_daily VALUES ('000001', ?)",
                [("2026-07-09",), ("2026-07-10",)],
            )

        class OneSessionClient:
            history_row_limit = 1

            def read(self, _stock_code):
                raise AssertionError("window must fail before source fetch")

        with self.assertRaisesRegex(ValueError, "history limit 1"):
            backfill_public_capital_v1(
                capital_client=OneSessionClient(),
                daily_client=_FakeBaoStockClient(),
                market_database_path=market,
                target_path=target,
                start_date="2026-07-09",
                end_date="2026-07-10",
                fetched_at="2026-07-28T12:00:00+08:00",
            )
        self.assertFalse(target.exists())

    @staticmethod
    def _bar(stock_code: str, pct_chg: float) -> StockDailyFactV1:
        return StockDailyFactV1(
            stock_code=stock_code,
            stock_name=None,
            trade_date="2026-07-09",
            open=None,
            high=None,
            low=None,
            close=None,
            preclose=None,
            volume=None,
            amount=None,
            pct_chg=pct_chg,
            turnover=None,
            is_st=None,
        )

    @staticmethod
    def _seed_supplemental(path: Path) -> None:
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE stock_capital_daily (
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    trade_date TEXT NOT NULL,
                    vendor_net_amount REAL,
                    float_market_cap REAL,
                    amount_unit TEXT NOT NULL,
                    market_cap_unit TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(stock_code, trade_date)
                );
                CREATE TABLE sector_membership_history (
                    stock_code TEXT NOT NULL,
                    stock_name TEXT,
                    sector_code TEXT NOT NULL,
                    sector_name TEXT,
                    sector_level TEXT NOT NULL,
                    valid_from TEXT NOT NULL,
                    valid_to_exclusive TEXT,
                    source TEXT NOT NULL,
                    source_version TEXT NOT NULL,
                    fetched_at TEXT NOT NULL,
                    PRIMARY KEY(stock_code, sector_code, sector_level, valid_from)
                );
                """
            )
            connection.execute(
                "INSERT INTO stock_capital_daily VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    "000001", "平安银行", "2026-07-09", 120, 24000,
                    "CNY_10K", "CNY_10K",
                    "tushare.moneyflow_ths+daily_basic", "tushare.2026-07.v1",
                    "2026-07-27T10:00:00+08:00",
                ),
            )
            rows = [(
                "000001", "平安银行", "801010.SI", "农林牧渔", "L2",
                "2020-01-01", "2026-07-01", "tushare.index_member_all",
                "sw2021.v1", "2026-07-27T10:00:00+08:00",
            )]
            rows.extend(
                (
                    code, None, "801020.SI", "银行", "L2",
                    "2026-07-01", None, "tushare.index_member_all",
                    "sw2021.v1", "2026-07-27T10:00:00+08:00",
                )
                for code in ("000001", "000002", "000003")
            )
            connection.executemany(
                "INSERT INTO sector_membership_history VALUES (?,?,?,?,?,?,?,?,?,?)",
                rows,
            )


class _FakeTushareClient:
    def query(self, api_name, *, params, fields):
        if api_name == "moneyflow_ths":
            trade_date = str(params["trade_date"])
            return tuple({
                "ts_code": f"{code}.SZ",
                "trade_date": trade_date,
                "name": code,
                "net_amount": 10.0,
            } for code in ("000001", "000002", "000003"))
        if api_name == "daily_basic":
            trade_date = str(params["trade_date"])
            return tuple({
                "ts_code": f"{code}.SZ",
                "trade_date": trade_date,
                "circ_mv": 1000.0,
            } for code in ("000001", "000002", "000003"))
        if api_name == "index_classify":
            return ({
                "index_code": "801020.SI",
                "industry_name": "银行",
                "level": "L2",
                "src": "SW2021",
            },)
        if api_name == "index_member_all":
            return tuple({
                "l2_code": "801020.SI",
                "l2_name": "银行",
                "ts_code": f"{code}.SZ",
                "name": code,
                "in_date": "20200101",
                "out_date": None,
                "is_new": "Y",
            } for code in ("000001", "000002", "000003"))
        raise AssertionError(api_name)


class _FakePublicCapitalClient:
    def read(self, stock_code):
        return ({
            "trade_date": "2026-07-09",
            "vendor_net_amount": 100_000,
            "vendor_net_ratio_percent": 0.1,
            "amount_unit": "CNY",
        },)


class _FakeBadRatioCapitalClient:
    def read(self, stock_code):
        return ({
            "trade_date": "2026-07-09",
            "vendor_net_amount": 100_000,
            "vendor_net_ratio_percent": 10,
            "amount_unit": "CNY",
        },)


class _FakeCapitalPrefetchClient:
    def __init__(self, cached):
        self.cached = set(cached)
        self.read_codes = []

    def has_capital_cache(self, stock_code):
        return stock_code in self.cached

    def read(self, stock_code):
        self.read_codes.append(stock_code)
        self.cached.add(stock_code)
        return ()


class _FakeBaoStockClient:
    def read(self, stock_code, start_date, end_date):
        return ({
            "trade_date": "2026-07-09",
            "close": 10,
            "volume": 10_000_000,
            "amount": 100_000_000,
            "turnover_percent": 5,
            "volume_unit": "SHARE",
            "amount_unit": "CNY",
            "turnover_unit": "PERCENT",
        },)


class _FakeLotsMislabeledAsSharesClient:
    def read(self, stock_code, start_date, end_date):
        return ({
            "trade_date": "2026-07-09",
            "close": 10,
            "volume": 100_000,
            "amount": 100_000_000,
            "turnover_percent": 5,
            "volume_unit": "SHARE",
            "amount_unit": "CNY",
            "turnover_unit": "PERCENT",
        },)


class _FakeSuspendedBaoStockClient:
    def read(self, stock_code, start_date, end_date):
        return ({
            "trade_date": "2026-07-09",
            "close": "10",
            "volume": "",
            "amount": "",
            "turnover_percent": "",
            "volume_unit": "SHARE",
            "amount_unit": "CNY",
            "turnover_unit": "PERCENT",
        },)


class _FakeCninfoClient:
    def read(self, stock_code, start_date, end_date):
        return ({
            "stock_name": stock_code,
            "classification_code": "008003",
            "industry_code": "S480301",
            "industry_l2_name": "银行",
            "valid_from": "2021-07-30",
        },)


if __name__ == "__main__":
    unittest.main()
