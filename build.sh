#!/bin/sh
# Builds a *Linux* binary.  For the Windows .exe run build.bat on Windows:
# PyInstaller does not cross-compile.
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    ./setup.sh
fi
.venv/bin/python -m pip install -r requirements-build.txt
.venv/bin/python -m PyInstaller --noconfirm --clean xhttp.spec
echo "Built: $(pwd)/dist/xhttp"
