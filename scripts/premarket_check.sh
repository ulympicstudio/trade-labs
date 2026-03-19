#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# Trade Labs — Premarket Go / No-Go Smoke Check
#
# Usage:
#   ./scripts/premarket_check.sh                    # run system ~60s, check, shutdown
#   ./scripts/premarket_check.sh /tmp/custom.log    # check existing log
#   PREMARKET_DURATION=90 ./scripts/premarket_check.sh  # custom duration
#
# Starts the system briefly (if no log provided), then asserts
# all critical subsystems are healthy before the market opens.
# Exit 0 = GO, Exit 1 = NO-GO (with reasons).
# ─────────────────────────────────────────────────────────────
set -euo pipefail

DURATION="${PREMARKET_DURATION:-60}"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="$ROOT_DIR/.venv/bin/activate"

fail_reasons=()
pass_count=0

gate() {
  local label="$1" pattern="$2" min="${3:-1}"
  local count
  count=$(grep -c "$pattern" "$LOG" 2>/dev/null || true)
  if [[ "$count" -ge "$min" ]]; then
    printf "  ✅  %-35s %s\n" "$label" "$count"
    ((pass_count++))
  else
    printf "  ❌  %-35s FAIL (need≥%s, got %s)\n" "$label" "$min" "$count"
    fail_reasons+=("$label")
  fi
}

# ── Decide: run fresh or use existing log ────────────────────────
if [[ -n "${1:-}" && -f "${1:-}" ]]; then
  LOG="$1"
  echo "Using existing log: $LOG"
else
  LOG="/tmp/tradelabs_premarket_check.log"
  echo "════════════════════════════════════════════════════"
  echo "  Starting system for ${DURATION}s smoke check..."
  echo "════════════════════════════════════════════════════"
  echo ""

  # Source venv
  if [[ -f "$VENV" ]]; then
    # shellcheck disable=SC1090
    source "$VENV"
  fi

  # Start system in background
  cd "$ROOT_DIR"
  PYTHONPATH="$ROOT_DIR" python -m src.arms.dev_all_in_one > "$LOG" 2>&1 &
  PID=$!

  # Wait for the configured duration
  sleep "$DURATION"

  # Graceful shutdown
  kill -INT "$PID" 2>/dev/null || true
  sleep 3
  kill -9 "$PID" 2>/dev/null || true
  wait "$PID" 2>/dev/null || true

  echo "  System stopped. Log: $LOG ($(wc -l < "$LOG") lines)"
  echo ""
fi

# ── Assertions ────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════"
echo "  Premarket Go / No-Go Gates"
echo "  Log: $LOG"
echo "════════════════════════════════════════════════════"
echo ""

echo "── Startup & Arms ──────────────────────────────"
gate "Clean startup"                "Signal arm starting\|All arms launched"
gate "Ingest heartbeat"             "ingest.*heartbeat\|arm=.ingest"
gate "Signal heartbeat"             "signal.*heartbeat\|arm=.signal"
gate "Risk heartbeat"               "risk.*heartbeat\|arm=.risk"
gate "Execution heartbeat"          "execution.*heartbeat\|arm=.execution"
echo ""

echo "── Data Flow ─────────────────────────────────"
gate "Snapshots received"           "snapshots_rx="                         1
gate "News poll executed"           "News poll\|polling"                    1
echo ""

echo "── Signal Pipeline ──────────────────────────"
gate "Session state logged"         "session_state="                        1
gate "Regime detection"             "regime="                               1
gate "EventScore computed"          "event_score"                           1
gate "Observability gates"          "obs_gates"                             1
echo ""

echo "── Risk Pipeline ────────────────────────────"
gate "Risk heartbeat logged"        "risk.*heartbeat"                       1
gate "Breakers check"               "breakers:"                             1
echo ""

echo "── Errors ────────────────────────────────────"
errs=$(grep -cE "Traceback|\[ERROR\]|CRITICAL" "$LOG" 2>/dev/null || true)
real_errs=$(grep -E "Traceback|\[ERROR\]|CRITICAL" "$LOG" 2>/dev/null | grep -cvE "resolved_fail|exception_handled|expected_error" || true)
if [[ "${real_errs:-0}" -eq 0 ]]; then
  printf "  ✅  %-35s 0\n" "No errors"
  ((pass_count++))
else
  printf "  ❌  %-35s %s errors\n" "Errors detected" "$real_errs"
  fail_reasons+=("errors_detected($real_errs)")
  echo "  📌 Last error:"
  grep -E "Traceback|\[ERROR\]|CRITICAL" "$LOG" | grep -vE "resolved_fail|exception_handled|expected_error" | tail -1 | head -c 120
  echo ""
fi

echo "── Shutdown ──────────────────────────────────"
gate "Clean shutdown"               "stopped\.\|Shutdown signal"            1
echo ""

# ── Verdict ───────────────────────────────────────────────────────
echo "════════════════════════════════════════════════════"
if [[ ${#fail_reasons[@]} -eq 0 ]]; then
  echo "  🟢 GO — All $pass_count gates passed."
  echo "════════════════════════════════════════════════════"
  exit 0
else
  echo "  🔴 NO-GO — ${#fail_reasons[@]} gate(s) failed:"
  for r in "${fail_reasons[@]}"; do
    echo "    • $r"
  done
  echo ""
  echo "  ($pass_count gates passed)"
  echo "════════════════════════════════════════════════════"
  exit 1
fi
