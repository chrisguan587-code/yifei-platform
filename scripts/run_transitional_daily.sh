#!/bin/sh
set -eu

if [ "$#" -ne 4 ]; then
  echo "usage: $0 SOURCE_DB SOURCE_HEALTH_ROOT TARGET_DB READINESS_ROOT" >&2
  exit 64
fi

SOURCE_DB="$1"
SOURCE_HEALTH_ROOT="$2"
TARGET_DB="$3"
READINESS_ROOT="$4"
AS_OF="$(date +%F)"
HEALTH_ARTIFACT="$SOURCE_HEALTH_ROOT/$AS_OF.json"
if [ ! -f "$HEALTH_ARTIFACT" ]; then
  echo "health artifact not found: $HEALTH_ARTIFACT" >&2
  exit 69
fi
PUBLISHED_AT="$(date -u '+%Y-%m-%dT%H:%M:%S+00:00')"

exec "$(dirname "$0")/../.venv/bin/yifei-platform-publish-transitional" \
  --source-db "$SOURCE_DB" \
  --source-health "$HEALTH_ARTIFACT" \
  --target-db "$TARGET_DB" \
  --readiness-root "$READINESS_ROOT" \
  --as-of "$AS_OF" \
  --published-at "$PUBLISHED_AT"
