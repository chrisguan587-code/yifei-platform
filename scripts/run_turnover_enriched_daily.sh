#!/bin/sh
set -eu

if [ "$#" -ne 6 ]; then
  echo "usage: $0 SOURCE_DB SOURCE_HEALTH_ROOT TURNOVER_ROOT FLOAT_SHARE_REFERENCE TARGET_DB READINESS_ROOT" >&2
  exit 64
fi

SOURCE_DB="$1"
SOURCE_HEALTH_ROOT="$2"
TURNOVER_ROOT="$3"
FLOAT_SHARE_REFERENCE="$4"
TARGET_DB="$5"
READINESS_ROOT="$6"
AS_OF="${AS_OF:-$(TZ=Asia/Shanghai date +%F)}"
HEALTH_ARTIFACT="$SOURCE_HEALTH_ROOT/$AS_OF.json"
TURNOVER_SNAPSHOT="$TURNOVER_ROOT/$AS_OF.json"
if [ ! -f "$HEALTH_ARTIFACT" ]; then
  echo "health artifact not found: $HEALTH_ARTIFACT" >&2
  exit 69
fi
FETCHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
SNAPSHOT_CLI="$(dirname "$0")/../.venv/bin/yifei-platform-turnover-snapshot"
PYTHON_BIN="$(dirname "$0")/../.venv/bin/python"

run_with_timeout() {
  timeout_seconds="$1"
  shift
  "$PYTHON_BIN" - "$timeout_seconds" "$@" <<'PY'
import subprocess
import sys
import os

timeout_seconds = float(sys.argv[1])
environment = os.environ.copy()
for name in (
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "http_proxy", "https_proxy", "all_proxy",
):
    environment.pop(name, None)
try:
    result = subprocess.run(
        sys.argv[2:], check=False, timeout=timeout_seconds, env=environment,
    )
except subprocess.TimeoutExpired:
    print(f"turnover provider timed out after {int(timeout_seconds)} seconds", file=sys.stderr)
    raise SystemExit(75)
raise SystemExit(result.returncode)
PY
}

validate_existing_snapshot() {
  source_version="$(
    "$(dirname "$0")/../.venv/bin/python" -c \
      'import json,sys; print(json.load(open(sys.argv[1], encoding="utf-8"))["source_version"])' \
      "$TURNOVER_SNAPSHOT"
  )"
  case "$source_version" in
    baostock-daily-turnover.v1)
      "$SNAPSHOT_CLI" \
        --market-db "$SOURCE_DB" \
        --as-of "$AS_OF" \
        --fetched-at "$FETCHED_AT" \
        --validate-existing-only \
        --output "$TURNOVER_SNAPSHOT" >/dev/null
      ;;
    eastmoney-kline-daily-turnover.v1)
      "$SNAPSHOT_CLI" \
        --market-db "$SOURCE_DB" \
        --as-of "$AS_OF" \
        --fetched-at "$FETCHED_AT" \
        --validate-existing-only \
        --output "$TURNOVER_SNAPSHOT" >/dev/null
      ;;
    baostock-float-share-derived-turnover.v1)
      "$SNAPSHOT_CLI" \
        --market-db "$SOURCE_DB" \
        --as-of "$AS_OF" \
        --fetched-at "$FETCHED_AT" \
        --float-share-reference "$FLOAT_SHARE_REFERENCE" \
        --validate-existing-only \
        --output "$TURNOVER_SNAPSHOT" >/dev/null
      ;;
    *)
      echo "unsupported turnover snapshot source version: $source_version" >&2
      exit 65
      ;;
  esac
}

if [ -f "$TURNOVER_SNAPSHOT" ]; then
  validate_existing_snapshot
  echo "reusing validated immutable turnover snapshot: $TURNOVER_SNAPSHOT"
else
  if run_with_timeout 300 "$SNAPSHOT_CLI" \
      --market-db "$SOURCE_DB" \
      --as-of "$AS_OF" \
      --fetched-at "$FETCHED_AT" \
      --exact-provider baostock \
      --output "$TURNOVER_SNAPSHOT"; then
    :
  else
    if [ -f "$TURNOVER_SNAPSHOT" ]; then
      validate_existing_snapshot
      echo "reusing validated snapshot created by a concurrent runner"
    else
      echo "warning: BaoStock turnover unavailable or invalid; trying Eastmoney" >&2
      if run_with_timeout 300 "$SNAPSHOT_CLI" \
          --market-db "$SOURCE_DB" \
          --as-of "$AS_OF" \
          --fetched-at "$FETCHED_AT" \
          --exact-provider eastmoney \
          --output "$TURNOVER_SNAPSHOT"; then
        :
      else
        echo "warning: Eastmoney turnover unavailable or invalid; using bounded reference" >&2
        if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
          -u http_proxy -u https_proxy -u all_proxy \
          "$SNAPSHOT_CLI" \
          --market-db "$SOURCE_DB" \
          --as-of "$AS_OF" \
          --fetched-at "$FETCHED_AT" \
          --float-share-reference "$FLOAT_SHARE_REFERENCE" \
          --output "$TURNOVER_SNAPSHOT"; then
          :
        else
          if [ -f "$TURNOVER_SNAPSHOT" ]; then
            validate_existing_snapshot
            echo "reusing validated fallback snapshot created by a concurrent runner"
          else
            PUBLISHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
            exec "$(dirname "$0")/../.venv/bin/yifei-platform-publish-transitional" \
              --source-db "$SOURCE_DB" \
              --source-health "$HEALTH_ARTIFACT" \
              --target-db "$TARGET_DB" \
              --readiness-root "$READINESS_ROOT" \
              --as-of "$AS_OF" \
              --published-at "$PUBLISHED_AT" \
              --turnover-reason-code turnover_sources_unavailable
          fi
        fi
      fi
    fi
  fi
fi

PUBLISHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
exec "$(dirname "$0")/../.venv/bin/yifei-platform-publish-turnover-enriched" \
  --source-db "$SOURCE_DB" \
  --source-health "$HEALTH_ARTIFACT" \
  --turnover-snapshot "$TURNOVER_SNAPSHOT" \
  --target-db "$TARGET_DB" \
  --readiness-root "$READINESS_ROOT" \
  --as-of "$AS_OF" \
  --published-at "$PUBLISHED_AT"
