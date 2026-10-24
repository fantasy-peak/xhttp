#!/bin/sh
set -eu
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if [ ! -x .venv/bin/python ]; then
    "$PYTHON" -m venv .venv
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
echo "Virtual environment ready: $(pwd)/.venv"
