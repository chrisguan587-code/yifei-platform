#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 MARKET_DB TARGET_DB MODE" >&2
  echo "MODE: weekly-shares | daily-market-cap" >&2
  exit 64
fi

MARKET_DB="$1"
TARGET_DB="$2"
MODE="$3"
AS_OF="${AS_OF:-$(TZ=Asia/Shanghai date +%F)}"
mkdir -p /Users/y-plus/projects/yifei/data/shared/logs
REPOSITORY_PYTHON="$(dirname "$0")/../.venv/bin/python"
if [ -n "${YIFEI_PLATFORM_PYTHON:-}" ]; then
  PYTHON_BIN="$YIFEI_PLATFORM_PYTHON"
elif [ -x "$REPOSITORY_PYTHON" ]; then
  PYTHON_BIN="$REPOSITORY_PYTHON"
else
  PYTHON_BIN="$(command -v python3)"
fi

UPDATED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
HAS_MARKET_ROWS="$("$PYTHON_BIN" - "$MARKET_DB" "$AS_OF" <<'PY'
import sqlite3
import sys
path, as_of = sys.argv[1], sys.argv[2]
with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
    row = connection.execute(
        "SELECT COUNT(*) FROM stock_daily WHERE trade_date=?", (as_of,)
    ).fetchone()
print("yes" if row and int(row[0]) > 0 else "no")
PY
)"
if [ "$HAS_MARKET_ROWS" != "yes" ]; then
  echo "capital facts publication skipped: $AS_OF has no stock_daily rows"
  exit 0
fi
case "$MODE" in
  weekly-shares)
    env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
        -u http_proxy -u https_proxy -u all_proxy \
        PYTHONPATH="$(dirname "$0")/../src" \
        "$PYTHON_BIN" -m yifei_platform.capital_facts_cli \
          publish-float-shares-weekly \
          --market-db "$MARKET_DB" \
          --target-db "$TARGET_DB" \
          --as-of "$AS_OF" \
          --updated-at "$UPDATED_AT"
    ;;
  daily-market-cap)
    env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
        -u http_proxy -u https_proxy -u all_proxy \
        PYTHONPATH="$(dirname "$0")/../src" \
        "$PYTHON_BIN" -m yifei_platform.capital_facts_cli \
          publish-float-market-cap-daily \
          --market-db "$MARKET_DB" \
          --target-db "$TARGET_DB" \
          --as-of "$AS_OF" \
          --updated-at "$UPDATED_AT"
    ;;
  *)
    echo "unsupported mode: $MODE" >&2
    exit 64
    ;;
esac
