#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT

cd "$root"
version=$(PYTHONPATH=src python3 -c 'import yifei_platform; print(yifei_platform.__version__)')
./scripts/quality_gate.sh
python3 -m build --outdir "$temporary/dist"

tar -xzf "$temporary/dist/yifei_platform-$version.tar.gz" -C "$temporary"
(
    cd "$temporary/yifei_platform-$version"
    PYTHONPATH=src python3 -m unittest discover -s tests
)

python3 -m venv "$temporary/venv"
"$temporary/venv/bin/pip" install --no-deps \
    "$temporary/dist/yifei_platform-$version-py3-none-any.whl"
"$temporary/venv/bin/python" -c \
    'import sys, yifei_platform; assert yifei_platform.__version__ == sys.argv[1]' "$version"
