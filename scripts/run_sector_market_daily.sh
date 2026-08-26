#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 MARKET_DB SUPPLEMENTAL_DB READINESS_ROOT" >&2
  exit 64
fi

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
python_bin="$root/.venv/bin/python"
AS_OF="${AS_OF:-$(TZ=Asia/Shanghai date +%F)}"

if "$python_bin" - "$1" "$AS_OF" <<'PY'
from pathlib import Path
import sqlite3
import sys

with sqlite3.connect(f"file:{Path(sys.argv[1]).resolve()}?mode=ro", uri=True) as connection:
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
    echo "non-trading date: sector market publication skipped ($AS_OF)"
    exit 0
  fi
  exit "$status"
fi

attempt=0
while [ "$attempt" -lt 480 ]; do
  if "$python_bin" - "$1" "$AS_OF" <<'PY'
from pathlib import Path
import sqlite3
import sys

with sqlite3.connect(f"file:{Path(sys.argv[1]).resolve()}?mode=ro", uri=True) as connection:
    stock = connection.execute(
        "SELECT 1 FROM stock_daily WHERE trade_date=? LIMIT 1", (sys.argv[2],)
    ).fetchone()
    breadth = connection.execute(
        "SELECT 1 FROM market_breadth_daily WHERE trade_date=?", (sys.argv[2],)
    ).fetchone()
sys.exit(0 if stock and breadth else 3)
PY
  then
    break
  fi
  status=$?
  if [ "$status" -ne 3 ]; then
    exit "$status"
  fi
  attempt=$((attempt + 1))
  sleep 30
done
if [ "$attempt" -ge 480 ]; then
  echo "sector market publication stopped: market facts missing for $AS_OF" >&2
  exit 75
fi

PUBLISHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
exec "$root/.venv/bin/yifei-platform-supplemental" \
  publish-sector-market-daily \
  --market-db "$1" \
  --target-db "$2" \
  --readiness-root "$3" \
  --as-of "$AS_OF" \
  --published-at "$PUBLISHED_AT"
