#!/bin/bash
set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate

echo "== Running unit tests (src/tests via pytest.ini) =="
set +e
python -m pytest
STATUS=$?
set -e

# pytest exit code 5 = "no tests collected" (not a real failure for now)
if [ "$STATUS" -ne 0 ] && [ "$STATUS" -ne 5 ]; then
  echo "❌ Unit tests failed (exit code $STATUS)"
  exit "$STATUS"
fi

echo ""
echo "== Running integration tests (optional) =="
python -m pytest test_sqlite_migration.py -q
python -m pytest test_bracket_live.py -q

echo ""
echo "✅ All checks complete"