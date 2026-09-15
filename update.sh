#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
mkdir -p data
exec .venv/bin/python update.py >> data/update.log 2>&1
