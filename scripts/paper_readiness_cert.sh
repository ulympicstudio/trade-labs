#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# U.T.S. Paper-Readiness Certification Script
#
# Runs dev_all_in_one with all force-paths enabled for a fixed window,
# captures log, then prints a compact metric summary.
#
# Usage:
#   bash scripts/paper_readiness_cert.sh [duration_seconds]
#
# Default duration: 90 seconds
# Log output: /tmp/tl_paper_cert.log
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

DURATION="${1:-90}"
LOG="/tmp/tl_paper_cert.log"
DIR="$(cd "$(dirname "$0")/.." && pwd)"

echo "╔══════════════════════════════════════════════════════╗"
echo "║   U.T.S. Paper-Readiness Certification              ║"
echo "║   Duration : ${DURATION}s                                    ║"
echo "║   Log      : ${LOG}                  ║"
echo "╚══════════════════════════════════════════════════════╝"
echo ""

# ── Environment: force-paths for deterministic paper pipeline ────────
export TL_CERT_MODE=true
export TL_TEST_FORCE_SESSION=RTH
export TL_TEST_FORCE_SECTOR_SCORE=80
export SIGNAL_FORCE_INTENT=true
export TL_TEST_FORCE_INTENT_INTERVAL=15

# Execution: paper fills only, no live orders
export EXECUTION_ENABLED=false
export TL_EXEC_FORCE_PAPER_FILL=true
export TL_EXEC_SIM_FRICTION=true
export TL_DEV_HARNESS_INTERVAL_S=15

# Exit engine: force TRIM_25 so exit_trim fires
export TL_EXIT_FORCE_ACTION=TRIM_25

# Attribution + Scorecard + Tuning: all enabled with monitor output
export TL_ATTRIB_ENABLED=1
export TL_ATTRIB_MONITOR_ENABLED=1
export TL_SCORECARD_ENABLED=1
export TL_SCORECARD_MONITOR_ENABLED=1
export TL_SCORECARD_MIN_TRADES=3
export TL_TUNING_ENABLED=1
export TL_TUNING_MONITOR_ENABLED=1
export TL_TUNING_FORCE_BUCKET=news
export TL_TUNING_FORCE_EDGE=positive
export TL_TUNING_FORCE_SAMPLE=20

# Exit + Monitor output
export TL_EXIT_MONITOR_ENABLED=1

# Bus: local only (no Redis)
export BUS_BACKEND=local

# Logging
export TL_LOG_LEVEL=INFO

# ── Launch ───────────────────────────────────────────────────────────
cd "$DIR"
echo "[$(date '+%H:%M:%S')] Starting dev_all_in_one (PID will follow)..."
python -u -m src.arms.dev_all_in_one > "$LOG" 2>&1 &
PID=$!
echo "[$(date '+%H:%M:%S')] PID=$PID — running for ${DURATION}s..."

# Wait for the configured duration
sleep "$DURATION"

# Stop gracefully
kill "$PID" 2>/dev/null || true
sleep 2
kill -9 "$PID" 2>/dev/null || true
echo "[$(date '+%H:%M:%S')] Process stopped."
echo ""

# ── Metric Summary ──────────────────────────────────────────────────
echo "════════════════ METRIC SUMMARY ════════════════"
for metric in \
    "paper_cert_start" \
    "Trade APPROVED" \
    "PAPER_FILL" \
    "exit_register " \
    "exit_decision " \
    "exit_trim " \
    "exit_full " \
    "exit_time_stop " \
    "exit_action_executed " \
    "attrib_open " \
    "attrib_fill " \
    "attrib_close " \
    "scorecard_open " \
    "scorecard_close " \
    "playbook_score_update " \
    "open_positions " \
    "exit_watchlist " \
    "heartbeat.*tick=" \
    "Traceback" \
    "ERROR" \
    "CRITICAL" \
    "Arm.*crashed"; do
  label=$(echo "$metric" | sed 's/ $//; s/\\.\\*/ /g')
  if echo "$metric" | grep -q '\.\*'; then
    count=$(grep -cE "$metric" "$LOG" 2>/dev/null || true)
  else
    count=$(grep -c "$metric" "$LOG" 2>/dev/null || true)
  fi
  printf "  %-30s = %s\n" "$label" "${count:-0}"
done
echo "════════════════════════════════════════════════"
echo ""

# ── Heartbeat per arm ────────────────────────────────────────────────
echo "Heartbeats per arm:"
for arm in ingest signal risk execution monitor; do
  count=$(grep -c "heartbeat.*arm=$arm\|$arm.*heartbeat\|arm_status.*$arm" "$LOG" 2>/dev/null || true)
  printf "  %-12s = %s\n" "$arm" "${count:-0}"
done
echo ""

echo "Log: $LOG"
echo "Run verifier: python scripts/verify_paper_readiness.py $LOG"
