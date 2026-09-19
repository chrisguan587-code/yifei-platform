from __future__ import annotations

import argparse
import json
from pathlib import Path

from .capital_facts import (
    BAOSTOCK_FLOAT_SHARES_SOURCE_VERSION,
    FLOAT_MARKET_CAP_SOURCE_VERSION,
    MOOTDX_FLOAT_SHARES_SOURCE_VERSION,
    BaoStockFloatShareClientV1,
    CapitalPublicationResultV1,
    MootdxFloatShareClientV1,
    publish_float_market_cap_daily_v1,
    publish_float_shares_weekly_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish shared float shares and float market cap facts."
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    shares = subparsers.add_parser("publish-float-shares-weekly")
    shares.add_argument("--market-db", type=Path, required=True)
    shares.add_argument("--target-db", type=Path, required=True)
    shares.add_argument("--as-of", required=True)
    shares.add_argument("--updated-at", required=True)
    shares.add_argument("--minimum-coverage", type=float, default=0.98)
    shares.add_argument("--backup-sample-size", type=int, default=80)
    shares.add_argument("--disable-baostock-check", action="store_true")

    cap = subparsers.add_parser("publish-float-market-cap-daily")
    cap.add_argument("--market-db", type=Path, required=True)
    cap.add_argument("--target-db", type=Path, required=True)
    cap.add_argument("--as-of", required=True)
    cap.add_argument("--updated-at", required=True)
    cap.add_argument("--minimum-coverage", type=float, default=0.98)

    args = parser.parse_args()
    if args.command == "publish-float-shares-weekly":
        backup = None if args.disable_baostock_check else BaoStockFloatShareClientV1()
        result = publish_float_shares_weekly_v1(
            client=MootdxFloatShareClientV1(),
            backup_client=backup,
            market_database_path=args.market_db,
            target_path=args.target_db,
            as_of=args.as_of,
            updated_at=args.updated_at,
            minimum_coverage=args.minimum_coverage,
            backup_sample_size=args.backup_sample_size,
        )
    elif args.command == "publish-float-market-cap-daily":
        result = publish_float_market_cap_daily_v1(
            market_database_path=args.market_db,
            target_path=args.target_db,
            as_of=args.as_of,
            updated_at=args.updated_at,
            minimum_coverage=args.minimum_coverage,
        )
    else:  # pragma: no cover
        raise AssertionError(args.command)
    print(json.dumps(_payload(result), ensure_ascii=False, indent=2))
    return 0


def _payload(result: CapitalPublicationResultV1) -> dict[str, object]:
    return {
        "dataset": result.dataset,
        "as_of": result.as_of,
        "row_count": result.row_count,
        "expected_count": result.expected_count,
        "coverage": result.coverage,
        "health_status": result.health_status,
        "missing_count": result.missing_count,
        "anomaly_count": result.anomaly_count,
        "source": result.source,
        "source_version": result.source_version,
        "target_path": str(result.target_path),
        "elapsed_seconds": result.elapsed_seconds,
    }


if __name__ == "__main__":
    raise SystemExit(main())
