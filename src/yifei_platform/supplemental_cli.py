from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sqlite3

from .supplemental_facts import (
    BOARD_DAILY_MINIMUM_ROWS,
    SUPPLEMENTAL_SCHEMA_VERSION,
    migrate_legacy_board_facts_v1,
    migrate_legacy_ths_membership_v1,
    publish_supplemental_readiness_v1,
)
from .board_daily_ingestion import (
    AkshareThsBoardDailyClientV1,
    BOARD_DAILY_SOURCE_VERSION,
    sync_board_daily_v1,
)
from .sector_market_ingestion import (
    MINIMUM_SECTOR_COUNT,
    SECTOR_MARKET_SOURCE_VERSION,
    publish_sector_market_daily_v1,
)
from .readiness import ReadinessMarkerV1, ReadinessStoreV1
from .public_data_ingestion import (
    CAPITAL_SOURCE_VERSION,
    PUBLIC_DATA_SOURCE_VERSION,
    MEMBERSHIP_SOURCE_VERSION,
    SINA_CAPITAL_SOURCE,
    SINA_CAPITAL_SOURCE_VERSION,
    AkshareIndustryHistoryClientV1,
    AksharePublicDataClientV1,
    BaoStockDailyClientV1,
    SinaCapitalFlowClientV1,
    backfill_public_capital_v1,
    backfill_cninfo_membership_v1,
    backfill_public_supplemental_v1,
    prefetch_public_capital_v1,
    prepare_public_cache_v1,
)
from .sector_flow_ingestion import (
    SECTOR_FLOW_SOURCE_VERSION,
    LevistockSectorFlowClientV1,
    publish_sector_flow_daily_v1,
)
from .tushare_ingestion import (
    TUSHARE_SOURCE_VERSION,
    TushareApiClientV1,
    backfill_tushare_supplemental_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish neutral capital, PIT membership, and board facts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    board = subparsers.add_parser("migrate-board")
    board.add_argument("--source-db", type=Path, required=True)
    board.add_argument("--target-db", type=Path, required=True)
    board.add_argument("--published-at", required=True)
    board.add_argument("--source-version", required=True)
    board.add_argument("--readiness-root", type=Path)

    membership_snapshot = subparsers.add_parser("migrate-ths-membership")
    membership_snapshot.add_argument("--source-db", type=Path, required=True)
    membership_snapshot.add_argument("--target-db", type=Path, required=True)
    membership_snapshot.add_argument("--valid-from", required=True)
    membership_snapshot.add_argument("--fetched-at", required=True)
    membership_snapshot.add_argument("--source-version", required=True)

    board_daily = subparsers.add_parser("sync-board-daily")
    board_daily.add_argument("--market-db", type=Path, required=True)
    board_daily.add_argument("--target-db", type=Path, required=True)
    board_daily.add_argument("--as-of", required=True)
    board_daily.add_argument("--fetched-at", required=True)
    board_daily.add_argument(
        "--source-version", default=BOARD_DAILY_SOURCE_VERSION,
    )
    board_daily.add_argument("--readiness-root", type=Path)

    sector_market = subparsers.add_parser("publish-sector-market-daily")
    sector_market.add_argument("--market-db", type=Path, required=True)
    sector_market.add_argument("--target-db", type=Path, required=True)
    sector_market.add_argument("--as-of", required=True)
    sector_market.add_argument("--published-at", required=True)
    sector_market.add_argument(
        "--source-version", default=SECTOR_MARKET_SOURCE_VERSION,
    )
    sector_market.add_argument("--readiness-root", type=Path)

    backfill = subparsers.add_parser("backfill-tushare")
    backfill.add_argument("--market-db", type=Path, required=True)
    backfill.add_argument("--target-db", type=Path, required=True)
    backfill.add_argument("--start-date", required=True)
    backfill.add_argument("--end-date", required=True)
    backfill.add_argument("--fetched-at", required=True)
    backfill.add_argument(
        "--source-version", default=TUSHARE_SOURCE_VERSION
    )
    backfill.add_argument(
        "--token-env", default="TUSHARE_TOKEN",
        help="Environment variable containing the token; token is never printed.",
    )
    backfill.add_argument("--readiness-root", type=Path)

    public = subparsers.add_parser("backfill-public")
    public.add_argument("--market-db", type=Path, required=True)
    public.add_argument("--target-db", type=Path, required=True)
    public.add_argument("--start-date", required=True)
    public.add_argument("--end-date", required=True)
    public.add_argument("--fetched-at", required=True)
    public.add_argument(
        "--source-version", default=PUBLIC_DATA_SOURCE_VERSION
    )
    public.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="Persistent batch cache; reuse the same path to resume a failed run.",
    )
    public.add_argument("--readiness-root", type=Path)

    capital = subparsers.add_parser("backfill-capital-public")
    capital.add_argument("--market-db", type=Path, required=True)
    capital.add_argument("--target-db", type=Path, required=True)
    capital.add_argument("--start-date", required=True)
    capital.add_argument("--end-date", required=True)
    capital.add_argument("--fetched-at", required=True)
    capital.add_argument(
        "--source-version", default=CAPITAL_SOURCE_VERSION
    )
    capital.add_argument("--cache-dir", type=Path, required=True)
    capital.add_argument("--readiness-root", type=Path)

    prefetch_capital = subparsers.add_parser("prefetch-capital-public")
    prefetch_capital.add_argument("--market-db", type=Path, required=True)
    prefetch_capital.add_argument("--start-date", required=True)
    prefetch_capital.add_argument("--end-date", required=True)
    prefetch_capital.add_argument(
        "--source-version", default=CAPITAL_SOURCE_VERSION
    )
    prefetch_capital.add_argument("--cache-dir", type=Path, required=True)
    prefetch_capital.add_argument("--batch-size", type=int, default=20)

    sina_capital = subparsers.add_parser("backfill-capital-sina")
    sina_capital.add_argument("--market-db", type=Path, required=True)
    sina_capital.add_argument("--target-db", type=Path, required=True)
    sina_capital.add_argument("--start-date", required=True)
    sina_capital.add_argument("--end-date", required=True)
    sina_capital.add_argument("--fetched-at", required=True)
    sina_capital.add_argument(
        "--source-version", default=SINA_CAPITAL_SOURCE_VERSION
    )
    sina_capital.add_argument("--cache-dir", type=Path, required=True)
    sina_capital.add_argument("--readiness-root", type=Path)

    prefetch_sina = subparsers.add_parser("prefetch-capital-sina")
    prefetch_sina.add_argument("--market-db", type=Path, required=True)
    prefetch_sina.add_argument("--start-date", required=True)
    prefetch_sina.add_argument("--end-date", required=True)
    prefetch_sina.add_argument(
        "--source-version", default=SINA_CAPITAL_SOURCE_VERSION
    )
    prefetch_sina.add_argument("--cache-dir", type=Path, required=True)
    prefetch_sina.add_argument("--batch-size", type=int, default=20)

    membership = subparsers.add_parser("backfill-membership-public")
    membership.add_argument("--market-db", type=Path, required=True)
    membership.add_argument("--target-db", type=Path, required=True)
    membership.add_argument("--start-date", required=True)
    membership.add_argument("--end-date", required=True)
    membership.add_argument("--fetched-at", required=True)
    membership.add_argument(
        "--source-version", default=MEMBERSHIP_SOURCE_VERSION
    )
    membership.add_argument(
        "--cache-dir",
        type=Path,
        required=True,
        help="Persistent batch cache; reuse the same path to resume a failed run.",
    )
    membership.add_argument("--readiness-root", type=Path)

    sector_flow = subparsers.add_parser("collect-sector-flow")
    sector_flow.add_argument("--market-db", type=Path, required=True)
    sector_flow.add_argument("--target-db", type=Path, required=True)
    sector_flow.add_argument(
        "--raw-snapshot-root", type=Path, required=True
    )
    sector_flow.add_argument("--as-of", required=True)
    sector_flow.add_argument("--fetched-at", required=True)
    sector_flow.add_argument(
        "--source-version", default=SECTOR_FLOW_SOURCE_VERSION
    )
    sector_flow.add_argument("--readiness-root", type=Path)

    args = parser.parse_args()
    if args.command == "migrate-board":
        result = migrate_legacy_board_facts_v1(
            source_path=args.source_db,
            target_path=args.target_db,
            published_at=args.published_at,
            source_version=args.source_version,
        )
        payload = {
            "dataset": "ths_board_daily",
            "latest_available_as_of": result.latest_available_as_of,
            "schema_version": result.schema_version,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = publish_supplemental_readiness_v1(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.latest_available_as_of,
                published_at=args.published_at,
                source_versions={
                    "ths_board_daily": args.source_version,
                },
                dataset_coverages={"ths_board_daily": None},
                bundle="v4-research-board",
            )
            payload["readiness_marker_id"] = marker.marker_id
    elif args.command == "migrate-ths-membership":
        result = migrate_legacy_ths_membership_v1(
            source_path=args.source_db,
            target_path=args.target_db,
            valid_from=args.valid_from,
            fetched_at=args.fetched_at,
            source_version=args.source_version,
        )
        payload = {
            "dataset": "sector_membership_history",
            "sector_level": "THS_L2",
            "valid_from": result.valid_from,
            "stock_count": result.stock_count,
            "board_count": result.board_count,
            "schema_version": result.schema_version,
            "target_path": str(result.target_path),
        }
    elif args.command == "sync-board-daily":
        result = sync_board_daily_v1(
            client=AkshareThsBoardDailyClientV1(),
            market_database_path=args.market_db,
            target_path=args.target_db,
            as_of=args.as_of,
            fetched_at=args.fetched_at,
            source_version=args.source_version,
        )
        payload = {
            "dataset": "ths_board_daily",
            "first_synced_date": result.first_synced_date,
            "inserted_row_count": result.inserted_row_count,
            "latest_available_as_of": result.latest_available_as_of,
            "synced_session_count": result.synced_session_count,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = _publish_board_readiness(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.latest_available_as_of,
                published_at=args.fetched_at,
                source_version=result.source_version,
                synced_session_count=result.synced_session_count,
            )
            payload["readiness_marker_id"] = marker.marker_id
    elif args.command == "publish-sector-market-daily":
        result = publish_sector_market_daily_v1(
            market_database_path=args.market_db,
            target_path=args.target_db,
            as_of=args.as_of,
            published_at=args.published_at,
            source_version=args.source_version,
        )
        payload = {
            "dataset": "sector_market_daily",
            "first_published_date": result.first_published_date,
            "inserted_row_count": result.inserted_row_count,
            "latest_available_as_of": result.latest_available_as_of,
            "published_session_count": result.published_session_count,
            "source_version": result.source_version,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = _publish_sector_market_readiness(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.latest_available_as_of,
                published_at=args.published_at,
                source_version=result.source_version,
                published_session_count=result.published_session_count,
            )
            payload["readiness_marker_id"] = marker.marker_id
    elif args.command == "backfill-tushare":
        token = os.environ.get(args.token_env, "")
        if not token:
            parser.error(
                f"environment variable {args.token_env} is not configured"
            )
        result = backfill_tushare_supplemental_v1(
            client=TushareApiClientV1(token),
            market_database_path=args.market_db,
            target_path=args.target_db,
            start_date=args.start_date,
            end_date=args.end_date,
            fetched_at=args.fetched_at,
            source_version=args.source_version,
        )
        payload = {
            "capital": "available",
            "membership": "available",
            "latest_capital_as_of": result.latest_capital_as_of,
            "membership_available_through": (
                result.membership_available_through
            ),
            "source_version": result.source_version,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = publish_supplemental_readiness_v1(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.latest_capital_as_of,
                published_at=args.fetched_at,
                source_versions={
                    "stock_capital_daily": result.source_version,
                    "sector_membership_history": result.source_version,
                },
                dataset_coverages={
                    "stock_capital_daily": None,
                    "sector_membership_history": None,
                },
                dataset_gate_coverages={
                    "stock_capital_daily": result.capital_coverage,
                    "sector_membership_history": result.membership_coverage,
                },
                bundle="v4-research-capital-sector",
            )
            payload["readiness_marker_id"] = marker.marker_id
    elif args.command == "backfill-public":
        prepare_public_cache_v1(
            cache_dir=args.cache_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            source_version=args.source_version,
        )
        akshare_client = AksharePublicDataClientV1(cache_dir=args.cache_dir)
        baostock_client = BaoStockDailyClientV1(cache_dir=args.cache_dir)
        try:
            result = backfill_public_supplemental_v1(
                capital_client=akshare_client,
                daily_client=baostock_client,
                industry_client=AkshareIndustryHistoryClientV1(
                    akshare_client
                ),
                market_database_path=args.market_db,
                target_path=args.target_db,
                start_date=args.start_date,
                end_date=args.end_date,
                fetched_at=args.fetched_at,
                source_version=args.source_version,
            )
        finally:
            baostock_client.close()
        payload = {
            "capital": "available",
            "membership": "available",
            "latest_capital_as_of": result.latest_capital_as_of,
            "membership_available_through": (
                result.membership_available_through
            ),
            "source_version": result.source_version,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = publish_supplemental_readiness_v1(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.latest_capital_as_of,
                published_at=args.fetched_at,
                source_versions={
                    "stock_capital_daily": result.source_version,
                    "sector_membership_history": result.source_version,
                },
                dataset_coverages={
                    "stock_capital_daily": None,
                    "sector_membership_history": None,
                },
                dataset_gate_coverages={
                    "stock_capital_daily": result.capital_coverage,
                    "sector_membership_history": result.membership_coverage,
                },
                bundle="v4-research-capital-sector",
            )
            payload["readiness_marker_id"] = marker.marker_id
    elif args.command == "prefetch-capital-public":
        prepare_public_cache_v1(
            cache_dir=args.cache_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            source_version=args.source_version,
        )
        result = prefetch_public_capital_v1(
            capital_client=AksharePublicDataClientV1(
                cache_dir=args.cache_dir
            ),
            market_database_path=args.market_db,
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
            source_version=args.source_version,
        )
        payload = {
            "dataset": "eastmoney_capital_raw_cache",
            "stock_count": result.stock_count,
            "cached_before": result.cached_before,
            "prefetched_count": result.prefetched_count,
            "remaining_count": result.remaining_count,
            "source_version": result.source_version,
        }
    elif args.command == "backfill-capital-public":
        prepare_public_cache_v1(
            cache_dir=args.cache_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            source_version=args.source_version,
        )
        capital_client = AksharePublicDataClientV1(cache_dir=args.cache_dir)
        daily_client = BaoStockDailyClientV1(cache_dir=args.cache_dir)
        try:
            result = backfill_public_capital_v1(
                capital_client=capital_client,
                daily_client=daily_client,
                market_database_path=args.market_db,
                target_path=args.target_db,
                start_date=args.start_date,
                end_date=args.end_date,
                fetched_at=args.fetched_at,
                source_version=args.source_version,
            )
        finally:
            daily_client.close()
        payload = {
            "capital": "available",
            "membership": "not_requested",
            "latest_capital_as_of": result.latest_capital_as_of,
            "stock_count": result.stock_count,
            "coverage": result.coverage,
            "source_version": result.source_version,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = publish_supplemental_readiness_v1(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.latest_capital_as_of,
                published_at=args.fetched_at,
                source_versions={
                    "stock_capital_daily": result.source_version,
                },
                dataset_coverages={
                    "stock_capital_daily": None,
                },
                dataset_gate_coverages={
                    "stock_capital_daily": result.coverage,
                },
                bundle="v4-research-stock-capital",
            )
            payload["readiness_marker_id"] = marker.marker_id
    elif args.command == "prefetch-capital-sina":
        prepare_public_cache_v1(
            cache_dir=args.cache_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            source_version=args.source_version,
        )
        result = prefetch_public_capital_v1(
            capital_client=SinaCapitalFlowClientV1(
                cache_dir=args.cache_dir
            ),
            market_database_path=args.market_db,
            start_date=args.start_date,
            end_date=args.end_date,
            batch_size=args.batch_size,
            source_version=args.source_version,
        )
        payload = {
            "dataset": "sina_capital_raw_cache",
            "stock_count": result.stock_count,
            "cached_before": result.cached_before,
            "prefetched_count": result.prefetched_count,
            "remaining_count": result.remaining_count,
            "source_version": result.source_version,
        }
    elif args.command == "backfill-capital-sina":
        prepare_public_cache_v1(
            cache_dir=args.cache_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            source_version=args.source_version,
        )
        capital_client = SinaCapitalFlowClientV1(cache_dir=args.cache_dir)
        daily_client = BaoStockDailyClientV1(cache_dir=args.cache_dir)
        try:
            result = backfill_public_capital_v1(
                capital_client=capital_client,
                daily_client=daily_client,
                market_database_path=args.market_db,
                target_path=args.target_db,
                start_date=args.start_date,
                end_date=args.end_date,
                fetched_at=args.fetched_at,
                source_version=args.source_version,
                capital_source=SINA_CAPITAL_SOURCE,
            )
        finally:
            daily_client.close()
        payload = {
            "capital": "available",
            "membership": "not_requested",
            "latest_capital_as_of": result.latest_capital_as_of,
            "stock_count": result.stock_count,
            "coverage": result.coverage,
            "source_version": result.source_version,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = publish_supplemental_readiness_v1(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.latest_capital_as_of,
                published_at=args.fetched_at,
                source_versions={
                    "stock_capital_daily": result.source_version,
                },
                dataset_coverages={
                    "stock_capital_daily": None,
                },
                dataset_gate_coverages={
                    "stock_capital_daily": result.coverage,
                },
                bundle="v4-research-stock-capital",
            )
            payload["readiness_marker_id"] = marker.marker_id
    elif args.command == "backfill-membership-public":
        prepare_public_cache_v1(
            cache_dir=args.cache_dir,
            start_date=args.start_date,
            end_date=args.end_date,
            source_version=args.source_version,
        )
        akshare_client = AksharePublicDataClientV1(cache_dir=args.cache_dir)
        result = backfill_cninfo_membership_v1(
            industry_client=AkshareIndustryHistoryClientV1(
                akshare_client
            ),
            market_database_path=args.market_db,
            target_path=args.target_db,
            start_date=args.start_date,
            end_date=args.end_date,
            fetched_at=args.fetched_at,
            source_version=args.source_version,
        )
        payload = {
            "capital": "not_requested",
            "membership": "available",
            "membership_available_through": (
                result.membership_available_through
            ),
            "stock_count": result.stock_count,
            "coverage": result.coverage,
            "source_version": result.source_version,
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = publish_supplemental_readiness_v1(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.membership_available_through,
                published_at=args.fetched_at,
                source_versions={
                    "sector_membership_history": result.source_version,
                },
                dataset_coverages={
                    "sector_membership_history": None,
                },
                dataset_gate_coverages={
                    "sector_membership_history": result.coverage,
                },
                bundle="v4-research-sector-membership",
            )
            payload["readiness_marker_id"] = marker.marker_id
    else:
        result = publish_sector_flow_daily_v1(
            client=LevistockSectorFlowClientV1(),
            market_database_path=args.market_db,
            target_path=args.target_db,
            raw_snapshot_root=args.raw_snapshot_root,
            as_of=args.as_of,
            fetched_at=args.fetched_at,
            source_version=args.source_version,
        )
        payload = {
            "dataset": "sector_fund_flow_daily",
            "as_of": result.as_of,
            "sector_count": result.sector_count,
            "source_version": result.source_version,
            "raw_snapshot_path": str(result.raw_snapshot_path),
            "target_path": str(result.target_path),
        }
        if args.readiness_root:
            marker = publish_supplemental_readiness_v1(
                database_path=result.target_path,
                readiness_root=args.readiness_root,
                as_of=result.as_of,
                published_at=args.fetched_at,
                source_versions={
                    "sector_fund_flow_daily": result.source_version,
                },
                dataset_coverages={
                    "sector_fund_flow_daily": None,
                },
                bundle="v4-research-sector-capital",
            )
            payload["readiness_marker_id"] = marker.marker_id
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


def _publish_board_readiness(
    *, database_path: Path, readiness_root: Path, as_of: str,
    published_at: str, source_version: str, synced_session_count: int,
) -> ReadinessMarkerV1:
    if synced_session_count == 0:
        existing = ReadinessStoreV1(readiness_root).read_ready(
            bundle="v4-research-board", as_of=as_of,
        )
        if existing is not None:
            _validate_reusable_board_readiness(
                marker=existing,
                database_path=database_path,
                as_of=as_of,
                source_version=source_version,
            )
            return existing
    return publish_supplemental_readiness_v1(
        database_path=database_path,
        readiness_root=readiness_root,
        as_of=as_of,
        published_at=published_at,
        source_versions={"ths_board_daily": source_version},
        dataset_coverages={"ths_board_daily": None},
        bundle="v4-research-board",
    )


def _publish_sector_market_readiness(
    *, database_path: Path, readiness_root: Path, as_of: str,
    published_at: str, source_version: str, published_session_count: int,
) -> ReadinessMarkerV1:
    if published_session_count == 0:
        existing = ReadinessStoreV1(readiness_root).read_ready(
            bundle="v4-market-sector", as_of=as_of,
        )
        if existing is not None:
            if (
                existing.bundle != "v4-market-sector"
                or existing.as_of != as_of
                or existing.producer_version != SUPPLEMENTAL_SCHEMA_VERSION
                or tuple(existing.required_datasets) != ("sector_market_daily",)
            ):
                raise ValueError("existing sector market readiness contract mismatch")
            with sqlite3.connect(
                f"file:{database_path.resolve(strict=True)}?mode=ro", uri=True,
            ) as connection:
                count = int(connection.execute(
                    """SELECT COUNT(*) FROM sector_market_daily
                       WHERE trade_date=? AND sector_level='THS_L2'
                         AND source_version=? AND amount_unit='CNY'""",
                    (as_of, source_version),
                ).fetchone()[0])
            if count < MINIMUM_SECTOR_COUNT:
                raise ValueError("existing sector market readiness coverage mismatch")
            return existing
    return publish_supplemental_readiness_v1(
        database_path=database_path,
        readiness_root=readiness_root,
        as_of=as_of,
        published_at=published_at,
        source_versions={"sector_market_daily": source_version},
        dataset_coverages={"sector_market_daily": None},
        bundle="v4-market-sector",
    )


def _validate_reusable_board_readiness(
    *, marker: ReadinessMarkerV1, database_path: Path,
    as_of: str, source_version: str,
) -> None:
    if (
        marker.bundle != "v4-research-board"
        or marker.as_of != as_of
        or marker.producer_version != SUPPLEMENTAL_SCHEMA_VERSION
        or marker.required_datasets != ("ths_board_daily",)
    ):
        raise ValueError("existing board readiness contract mismatch")
    with sqlite3.connect(
        f"file:{database_path.resolve(strict=True)}?mode=ro", uri=True,
    ) as connection:
        count = int(connection.execute(
            "SELECT COUNT(*) FROM ths_board_daily WHERE trade_date=?", (as_of,),
        ).fetchone()[0])
        metadata = connection.execute(
            "SELECT value FROM supplemental_metadata "
            "WHERE key='board_source_version'"
        ).fetchone()
    if count < BOARD_DAILY_MINIMUM_ROWS:
        raise ValueError("existing board readiness coverage mismatch")
    if metadata is None or str(metadata[0]) != source_version:
        raise ValueError("existing board readiness source version mismatch")


if __name__ == "__main__":
    raise SystemExit(main())
