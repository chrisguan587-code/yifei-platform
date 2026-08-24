#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 MARKET_DB SUPPLEMENTAL_DB READINESS_ROOT" >&2
  exit 64
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin="$root/.venv/bin/python"
AS_OF="${AS_OF:-$(TZ=Asia/Shanghai date +%F)}"
FETCHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"

if "$python_bin" - "$1" "$AS_OF" <<'PY'
from pathlib import Path
import sqlite3
import sys

database = Path(sys.argv[1]).resolve()
with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
    row = connection.execute(
        "SELECT 1 FROM trading_calendar WHERE trade_date=?", (sys.argv[2],)
    ).fetchone()
sys.exit(0 if row else 3)
PY
then
  :
else
  status=$?
  if [ "$status" -eq 3 ]; then
    echo "non-trading date: board publication skipped ($AS_OF)"
    exit 0
  fi
  exit "$status"
fi

exec env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
  -u http_proxy -u https_proxy -u all_proxy \
  "$root/.venv/bin/yifei-platform-supplemental" \
  sync-board-daily \
  --market-db "$1" \
  --target-db "$2" \
  --readiness-root "$3" \
  --as-of "$AS_OF" \
  --fetched-at "$FETCHED_AT"
