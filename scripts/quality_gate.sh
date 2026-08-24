#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$root"
PYTHON_BIN="${PYTHON_BIN:-$root/.venv/bin/python}"
"$PYTHON_BIN" -m compileall -q src tests
PYTHONPATH=src "$PYTHON_BIN" -m unittest discover -s tests -v
git diff --check
