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
AS_OF="$(date +%F)"
HEALTH_ARTIFACT="$SOURCE_HEALTH_ROOT/$AS_OF.json"
TURNOVER_SNAPSHOT="$TURNOVER_ROOT/$AS_OF.json"
if [ ! -f "$HEALTH_ARTIFACT" ]; then
  echo "health artifact not found: $HEALTH_ARTIFACT" >&2
  exit 69
fi
FETCHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"
SNAPSHOT_CLI="$(dirname "$0")/../.venv/bin/yifei-platform-turnover-snapshot"

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
        --output "$TURNOVER_SNAPSHOT" >/dev/null
      ;;
    baostock-float-share-derived-turnover.v1)
      "$SNAPSHOT_CLI" \
        --market-db "$SOURCE_DB" \
        --as-of "$AS_OF" \
        --fetched-at "$FETCHED_AT" \
        --float-share-reference "$FLOAT_SHARE_REFERENCE" \
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
  if env -u HTTP_PROXY -u HTTPS_PROXY -u ALL_PROXY \
    "$SNAPSHOT_CLI" \
      --market-db "$SOURCE_DB" \
      --as-of "$AS_OF" \
      --fetched-at "$FETCHED_AT" \
      --output "$TURNOVER_SNAPSHOT"; then
    :
  else
    status="$?"
    if [ -f "$TURNOVER_SNAPSHOT" ]; then
      validate_existing_snapshot
      echo "reusing validated snapshot created by a concurrent runner"
    elif [ "$status" -ne 75 ]; then
      echo "exact-date turnover failed with non-fallback status: $status" >&2
      exit "$status"
    else
      echo "warning: exact-date BaoStock unavailable; using bounded reference" >&2
      if "$SNAPSHOT_CLI" \
        --market-db "$SOURCE_DB" \
        --as-of "$AS_OF" \
        --fetched-at "$FETCHED_AT" \
        --float-share-reference "$FLOAT_SHARE_REFERENCE" \
        --output "$TURNOVER_SNAPSHOT"; then
        :
      else
        fallback_status="$?"
        if [ -f "$TURNOVER_SNAPSHOT" ]; then
          validate_existing_snapshot
          echo "reusing validated fallback snapshot created by a concurrent runner"
        else
          exit "$fallback_status"
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
