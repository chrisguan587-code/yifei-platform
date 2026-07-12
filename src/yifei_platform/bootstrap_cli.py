from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bootstrap import bootstrap_market_data


def main() -> int:
    parser = argparse.ArgumentParser(
        description="One-time migration of legacy stock_daily facts into Platform storage."
    )
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--target-db", type=Path, required=True)
    parser.add_argument("--readiness-root", type=Path, required=True)
    parser.add_argument("--published-at", required=True)
    args = parser.parse_args()
    result = bootstrap_market_data(
        source_path=args.source_db,
        target_path=args.target_db,
        readiness_root=args.readiness_root,
        published_at=args.published_at,
    )
    print(json.dumps({
        "as_of": result.as_of,
        "database_sha256": result.database_sha256,
        "readiness_marker_id": result.readiness_marker.marker_id,
        "row_count": result.row_count,
        "session_count": result.session_count,
        "target_path": str(result.target_path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
