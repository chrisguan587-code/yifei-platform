from __future__ import annotations

import argparse
import json
from pathlib import Path

from .turnover_ingestion import (
    build_float_share_reference_from_turnover_snapshot_v1,
    build_float_share_reference_v1,
    write_float_share_reference_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish an immutable audited BaoStock float-share reference."
    )
    parser.add_argument("--market-db", type=Path, required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--capital-db", type=Path)
    source.add_argument("--turnover-snapshot", type=Path)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--created-at", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.turnover_snapshot:
        snapshot = json.loads(
            args.turnover_snapshot.read_text(encoding="utf-8")
        )
        if snapshot.get("as_of") != args.as_of:
            parser.error("turnover snapshot date does not match --as-of")
        payload = build_float_share_reference_from_turnover_snapshot_v1(
            market_database_path=args.market_db,
            snapshot=snapshot,
            created_at=args.created_at,
        )
    else:
        payload = build_float_share_reference_v1(
            market_database_path=args.market_db,
            capital_database_path=args.capital_db,
            as_of=args.as_of,
            created_at=args.created_at,
        )
    write_float_share_reference_v1(payload=payload, output=args.output)
    print(json.dumps({
        "as_of": payload["as_of"],
        "output": str(args.output),
        "row_count": payload["row_count"],
        "source_version": payload["source_version"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
