#!/bin/sh
set -eu
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
as_of="${AS_OF:-$(TZ=Asia/Shanghai date +%F)}"
if "$root/.venv/bin/python" - "$1" "$as_of" <<'PY'
import sqlite3,sys
from pathlib import Path
with sqlite3.connect(f'file:{Path(sys.argv[1]).resolve()}?mode=ro',uri=True) as c:
    session=c.execute('SELECT 1 FROM trading_calendar WHERE trade_date=?',(sys.argv[2],)).fetchone()
sys.exit(0 if session else 3)
PY
then
  :
else
  status=$?
  if [ "$status" -eq 3 ]; then
    echo "non-trading date: vendor index skipped ($as_of)"
    exit 0
  fi
  exit "$status"
fi
exec "$root/.venv/bin/yifei-platform-vendor-index" --market-db "$1" --target-db "$2" \
  --snapshot-root "$3" --as-of "$as_of"
