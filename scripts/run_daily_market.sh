#!/bin/sh
set -eu

if [ "$#" -ne 3 ]; then
  echo "usage: $0 EXCHANGE_CALENDAR TARGET_DB READINESS_ROOT" >&2
  exit 64
fi

EXCHANGE_CALENDAR="$1"
TARGET_DB="$2"
READINESS_ROOT="$3"
AS_OF="${AS_OF:-$(TZ=Asia/Shanghai date +%F)}"
REPOSITORY_PYTHON="$(dirname "$0")/../.venv/bin/python"
REPOSITORY_PUBLISHER="$(dirname "$0")/../.venv/bin/yifei-platform-publish-daily-market"
if [ -n "${YIFEI_PLATFORM_PYTHON:-}" ]; then
  PYTHON_BIN="$YIFEI_PLATFORM_PYTHON"
elif [ -x "$REPOSITORY_PYTHON" ]; then
  PYTHON_BIN="$REPOSITORY_PYTHON"
else
  PYTHON_BIN="$(command -v python3)"
fi
IS_SESSION="$("$PYTHON_BIN" - "$EXCHANGE_CALENDAR" "$AS_OF" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1]).resolve(strict=True)
as_of = sys.argv[2]
payload = json.loads(path.read_text(encoding="utf-8"))
if payload.get("schema_version") != "exchange-trading-calendar.v1":
    raise SystemExit("unsupported exchange calendar")
print("yes" if as_of in payload["sessions"] else "no")
PY
)"
if [ "$IS_SESSION" != "yes" ]; then
  echo "daily market publication skipped: $AS_OF is not an exchange session"
  exit 0
fi

if [ -x "$REPOSITORY_PUBLISHER" ]; then
  PUBLISHER="$REPOSITORY_PUBLISHER"
else
  PUBLISHER="$(command -v yifei-platform-publish-daily-market)"
fi

attempt=1
while [ "$attempt" -le 3 ]; do
  PUBLISHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
  if env \
      -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
      -u http_proxy -u https_proxy -u all_proxy \
      "$PUBLISHER" \
        --target-db "$TARGET_DB" \
        --readiness-root "$READINESS_ROOT" \
        --as-of "$AS_OF" \
        --published-at "$PUBLISHED_AT"; then
    exit 0
  fi
  if [ "$attempt" -lt 3 ]; then
    sleep 30
  fi
  attempt=$((attempt + 1))
done
echo "daily market publication failed after 3 attempts: $AS_OF" >&2
exit 75
