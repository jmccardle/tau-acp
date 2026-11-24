#!/usr/bin/env bash
# The gate: lint, format, complexity, tests.
set -euo pipefail
cd "$(dirname "$0")/.."
ruff check
ruff format --check
python scripts/check_cc.py
pytest -q
