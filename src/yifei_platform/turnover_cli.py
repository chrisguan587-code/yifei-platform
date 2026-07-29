from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

from .public_data_ingestion import BaoStockDailyClientV1
from .turnover_ingestion import (
    BAOSTOCK_TURNOVER_SCHEMA_VERSION,
    BAOSTOCK_TURNOVER_SOURCE_VERSION,
    DERIVED_TURNOVER_SOURCE_VERSION,
    build_derived_turnover_snapshot_v1,
    build_baostock_turnover_snapshot_v1,
    float_share_reference_identity_v1,
    write_turnover_snapshot_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Collect one immutable exact-date BaoStock turnover snapshot."
    )
    parser.add_argument("--market-db", type=Path, required=True)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--fetched-at", required=True)
    parser.add_argument("--float-share-reference", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        payload = json.loads(args.output.read_text(encoding="utf-8"))
        expected_source_version = (
            DERIVED_TURNOVER_SOURCE_VERSION
            if args.float_share_reference
            else BAOSTOCK_TURNOVER_SOURCE_VERSION
        )
        if (
            payload.get("schema_version")
            != BAOSTOCK_TURNOVER_SCHEMA_VERSION
            or payload.get("as_of") != args.as_of
            or payload.get("source_version") != expected_source_version
        ):
            parser.error("existing turnover snapshot identity mismatch")
        if args.float_share_reference:
            reference = json.loads(
                args.float_share_reference.read_text(encoding="utf-8")
            )
            reference_as_of, reference_source_version, reference_hash = (
                float_share_reference_identity_v1(reference)
            )
            if (
                payload.get("reference_as_of") != reference_as_of
                or payload.get("reference_source_version")
                != reference_source_version
                or payload.get("reference_content_sha256") != reference_hash
            ):
                parser.error(
                    "existing turnover snapshot reference mismatch"
                )
            expected_payload = build_derived_turnover_snapshot_v1(
                market_database_path=args.market_db,
                as_of=args.as_of,
                fetched_at=str(payload.get("fetched_at") or ""),
                reference=reference,
            )
            if payload != expected_payload:
                parser.error(
                    "existing turnover snapshot market-db mismatch"
                )
        reused = True
    else:
        if args.float_share_reference:
            reference = json.loads(
                args.float_share_reference.read_text(encoding="utf-8")
            )
            payload = build_derived_turnover_snapshot_v1(
                market_database_path=args.market_db,
                as_of=args.as_of,
                fetched_at=args.fetched_at,
                reference=reference,
            )
        else:
            try:
                client = BaoStockDailyClientV1(retry_attempts=3)
                try:
                    payload = build_baostock_turnover_snapshot_v1(
                        market_database_path=args.market_db,
                        as_of=args.as_of,
                        fetched_at=args.fetched_at,
                        client=client,
                    )
                finally:
                    client.close()
            except RuntimeError as exc:
                print(
                    f"exact-date BaoStock turnover unavailable: {exc}",
                    file=sys.stderr,
                )
                return os.EX_TEMPFAIL
        write_turnover_snapshot_v1(
            payload=payload,
            output=args.output,
        )
        reused = False
    print(json.dumps({
        "as_of": payload["as_of"],
        "coverage": payload["summary"]["coverage"],
        "covered_row_count": payload["summary"]["covered_row_count"],
        "eligible_row_count": payload["summary"]["eligible_row_count"],
        "output": str(args.output),
        "reused": reused,
        "source_version": payload["source_version"],
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
