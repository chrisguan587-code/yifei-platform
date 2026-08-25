from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path
import sqlite3

from .daily_market import AkshareCsi300DailyClientV1
from .market_observation import migrate_market_observation_facts_v1


LOGGER = logging.getLogger(__name__)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate CSI 300 history and derive Platform market breadth facts."
    )
    parser.add_argument("--target-db", type=Path, required=True)
    parser.add_argument("--legacy-index-db", type=Path, required=True)
    parser.add_argument("--published-at", required=True)
    args = parser.parse_args()
    target = args.target_db.resolve(strict=True)
    with sqlite3.connect(f"{target.as_uri()}?mode=ro", uri=True) as connection:
        target_as_of = str(connection.execute(
            "SELECT MAX(trade_date) FROM stock_daily"
        ).fetchone()[0])
    index_client = AkshareCsi300DailyClientV1()
    try:
        latest_index_row = index_client.fetch(as_of=target_as_of)
    except Exception:
        latest_index_row = None
        LOGGER.warning(
            "CSI 300 exact-date fetch failed during migration; "
            "continuing with available history",
            exc_info=True,
        )
    result = migrate_market_observation_facts_v1(
        target_path=target,
        legacy_index_path=args.legacy_index_db,
        published_at=args.published_at,
        latest_index_row=latest_index_row,
        latest_index_source_version=index_client.source_version,
    )
    print(json.dumps({
        "target_path": str(result.target_path),
        "index_row_count": result.index_row_count,
        "breadth_row_count": result.breadth_row_count,
        "min_trade_date": result.min_trade_date,
        "max_trade_date": result.max_trade_date,
        "database_sha256": result.database_sha256,
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
