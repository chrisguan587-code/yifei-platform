from __future__ import annotations

import argparse
import json
from pathlib import Path

from .bootstrap import _sha256
from .daily_market import (
    PlatformCsi300DailyClientV1,
    PlatformDailySnapshotClientV1,
    publish_platform_daily_market_data,
    repair_recent_missing_csi300_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish one Platform-owned post-close A-share snapshot."
    )
    parser.add_argument("--target-db", type=Path, required=True)
    parser.add_argument("--readiness-root", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--published-at", required=True)
    args = parser.parse_args()
    result = publish_platform_daily_market_data(
        client=PlatformDailySnapshotClientV1(),
        target_path=args.target_db,
        readiness_root=args.readiness_root,
        as_of=args.as_of,
        published_at=args.published_at,
        index_client=PlatformCsi300DailyClientV1(),
    )
    corrections = repair_recent_missing_csi300_v1(
        target_path=args.target_db,
        corrected_at=args.published_at,
        client_factory=PlatformCsi300DailyClientV1,
    )
    print(json.dumps({
        "as_of": result.as_of,
        "index_corrections": corrections,
        "database_sha256": _sha256(args.target_db),
        "readiness_marker_id": result.readiness_marker.marker_id,
        "row_count": result.row_count,
        "session_count": result.session_count,
        "target_path": str(result.target_path),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
