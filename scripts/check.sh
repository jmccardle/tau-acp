#!/usr/bin/env bash
# The gate: lint, format, tests.
set -euo pipefail
cd "$(dirname "$0")/.."
ruff check
ruff format --check
pytest -q
