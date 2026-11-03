#!/bin/sh
set -eu
if [ "$#" -ne 2 ]; then
  echo "usage: $0 EXCHANGE_CALENDAR SNAPSHOT_ROOT" >&2
  exit 64
fi
root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec env PYTHONPATH="$root/src" \
  "$root/.venv/bin/python" -m yifei_platform.attention_capture \
  --exchange-calendar "$1" --snapshot-root "$2" \
  --representatives "$root/config/stock_representatives.v1.json"
