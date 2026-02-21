#!/bin/bash
set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate

echo "== Unit sanity checks =="
# Run only stable tests for now
python -m pytest -q test_sqlite_migration.py

echo ""
echo "== Integration tests =="
python -m pytest -q test_bracket_live.py

echo ""
echo "✅ All checks complete"