from __future__ import annotations

import argparse
import json
from pathlib import Path

from .exchange_calendar_publish import (
    build_exchange_calendar_manifest_v1,
    write_exchange_calendar_manifest_v1,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Publish one independent explicit exchange calendar manifest."
    )
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    source = json.loads(args.source.read_text(encoding="utf-8"))
    payload = build_exchange_calendar_manifest_v1(
        source=source, published_at=args.published_at
    )
    reused = write_exchange_calendar_manifest_v1(
        payload=payload, target=args.output
    )
    print(json.dumps({
        "coverage_end": payload["coverage_end"],
        "coverage_start": payload["coverage_start"],
        "output": str(args.output),
        "reused": reused,
        "schema_version": payload["schema_version"],
        "session_count": len(payload["sessions"]),
        "source_version": payload["source_version"],
    }, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
