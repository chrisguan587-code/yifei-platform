#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
temporary=$(mktemp -d)
trap 'rm -rf "$temporary"' EXIT
python_bin="${PYTHON_BIN:-$root/.venv/bin/python}"

cd "$root"
version=$(PYTHONPATH=src "$python_bin" -c 'import yifei_platform; print(yifei_platform.__version__)')
./scripts/quality_gate.sh
(
    cd "$temporary"
    "$python_bin" -m build "$root" --outdir "$temporary/dist"
)

tar -xzf "$temporary/dist/yifei_platform-$version.tar.gz" -C "$temporary"
(
    cd "$temporary/yifei_platform-$version"
    PYTHONPATH=src "$python_bin" -m unittest discover -s tests
)

"$python_bin" -m venv "$temporary/venv"
"$temporary/venv/bin/pip" install --no-deps \
    "$temporary/dist/yifei_platform-$version-py3-none-any.whl"
"$temporary/venv/bin/python" -c \
    'import sys, yifei_platform; assert yifei_platform.__version__ == sys.argv[1]' "$version"
