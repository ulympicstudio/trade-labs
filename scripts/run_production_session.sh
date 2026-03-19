#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
#  TRADE-LABS · Production Session Launcher (Mac Studio 24hr)
#
#  Runs the arm architecture (dev_all_in_one.py) with:
#    • IB paper connection on port 7497
#    • Auto-restart on crash (up to MAX_RESTARTS per day)
#    • Log rotation (new log file each restart, cleanup after 7 days)
#    • Clean environment (all force flags disabled)
#
#  Usage:
#    ./scripts/run_production_session.sh
#
#  To stop: kill the process or Ctrl+C
#  Logs: logs/production_YYYYMMDD_HHMMSS.log
# ──────────────────────────────────────────────────────────────────────
set -uo pipefail

cd "$(dirname "$0")/.."

MAX_RESTARTS=10          # max restarts per calendar day
RESTART_DELAY_S=30       # wait before restarting after crash
LOG_RETENTION_DAYS=7     # delete logs older than this

# ── 1. Unset ALL force/test override flags ─────────────────────────

# Session & pipeline gate overrides
unset FORCE_SESSION               2>/dev/null || true
unset TL_TEST_FORCE_SESSION       2>/dev/null || true
unset TL_CERT_MODE                2>/dev/null || true
unset TL_FIRST_PAPER_SESSION      2>/dev/null || true

# Signal force flags
unset SIGNAL_FORCE_INTENT         2>/dev/null || true
unset TL_TEST_FORCE_INTENT_INTERVAL 2>/dev/null || true
unset SIGNAL_DEV_STRATEGY         2>/dev/null || true
unset TL_TEST_FORCE_EVENT_SCORE   2>/dev/null || true
unset TL_TEST_FORCE_CONSENSUS     2>/dev/null || true
unset TL_TEST_FORCE_SPREAD_PCT    2>/dev/null || true
unset TL_TEST_FORCE_REGIME        2>/dev/null || true
unset TL_TEST_FORCE_ATR_SPIKE     2>/dev/null || true
unset TL_TEST_FORCE_SQUEEZE_SCORE 2>/dev/null || true
unset TL_TEST_FORCE_SECTOR        2>/dev/null || true
unset TL_TEST_FORCE_SECTOR_STATE  2>/dev/null || true
unset TL_TEST_FORCE_SECTOR_SCORE  2>/dev/null || true
unset TL_TEST_FORCE_INDUSTRY      2>/dev/null || true
unset TL_TEST_FORCE_ROTATION_STATE 2>/dev/null || true
unset TL_TEST_FORCE_ROTATION_SCORE 2>/dev/null || true
unset TL_NEWS_BURST_FORCE_INCLUDE 2>/dev/null || true

# Volatility engine force flags
unset TL_VOL_FORCE_SCORE          2>/dev/null || true
unset TL_VOL_FORCE_STATE          2>/dev/null || true
unset TL_VOL_FORCE_RVOL           2>/dev/null || true
unset TL_VOL_FORCE_ATRX           2>/dev/null || true
unset TL_VOL_FORCE_SYMBOL         2>/dev/null || true

# Market mode & allocation force flags
unset TL_MODE_FORCE               2>/dev/null || true
unset TL_MODE_FORCE_CONFIDENCE    2>/dev/null || true
unset TL_MODE_FORCE_NEWS          2>/dev/null || true
unset TL_MODE_FORCE_ROTATION      2>/dev/null || true
unset TL_MODE_FORCE_VOL           2>/dev/null || true
unset TL_MODE_FORCE_MEANREV       2>/dev/null || true
unset TL_MODE_FORCE_CAP_MULT      2>/dev/null || true
unset TL_ALLOC_FORCE_MODE         2>/dev/null || true
unset TL_ALLOC_FORCE_NEWS         2>/dev/null || true
unset TL_ALLOC_FORCE_ROTATION     2>/dev/null || true
unset TL_ALLOC_FORCE_VOL          2>/dev/null || true
unset TL_ALLOC_FORCE_MEANREV      2>/dev/null || true
unset TL_ALLOC_FORCE_MAX_POSITIONS 2>/dev/null || true

# Execution force flags
unset TL_EXEC_FORCE_PAPER_FILL    2>/dev/null || true
unset TL_DEV_HARNESS_INTERVAL_S   2>/dev/null || true
unset TL_EXEC_SIM_FRICTION        2>/dev/null || true
unset TL_EXEC_SIM_DELAY_MS        2>/dev/null || true
unset TL_EXEC_SIM_SLIPPAGE_BPS    2>/dev/null || true
unset TL_EXEC_SIM_PARTIAL_FILL_PCT 2>/dev/null || true

# Exit intelligence force flags
unset TL_EXIT_FORCE_PLAYBOOK      2>/dev/null || true
unset TL_EXIT_FORCE_MODE          2>/dev/null || true
unset TL_EXIT_FORCE_ACTION        2>/dev/null || true
unset TL_EXIT_FORCE_MFE           2>/dev/null || true
unset TL_EXIT_FORCE_MAE           2>/dev/null || true
unset TL_EXIT_FORCE_PNL           2>/dev/null || true

# Scorecard & tuning force flags
unset TL_SCORECARD_FORCE_PLAYBOOK 2>/dev/null || true
unset TL_SCORECARD_FORCE_SCORE    2>/dev/null || true
unset TL_TUNING_FORCE_BUCKET      2>/dev/null || true
unset TL_TUNING_FORCE_EDGE        2>/dev/null || true
unset TL_TUNING_FORCE_SAMPLE      2>/dev/null || true
unset TL_TUNING_FORCE_WEIGHT_DELTA 2>/dev/null || true
unset TL_TUNING_FORCE_THRESHOLD_DELTA 2>/dev/null || true

# Dev pipeline force flags
unset TL_FORCE_INGEST_TICK        2>/dev/null || true
unset TL_FORCE_SIGNAL_FIRE        2>/dev/null || true
unset TL_FORCE_RISK_APPROVE       2>/dev/null || true
unset TL_FORCE_EXEC_FILL          2>/dev/null || true
unset TL_FORCE_EXIT_TRIM          2>/dev/null || true
unset TL_FORCE_EXIT_FULL          2>/dev/null || true
unset TL_FORCE_SCORECARD_UPDATE   2>/dev/null || true
unset TL_FORCE_ATTRIB_OPEN        2>/dev/null || true
unset TL_FORCE_ATTRIB_FILL        2>/dev/null || true
unset TL_FORCE_ATTRIB_CLOSE       2>/dev/null || true
unset TL_FORCE_EXIT_REGISTER      2>/dev/null || true

# Kill switch force
unset TRADE_LABS_FORCE_KILL        2>/dev/null || true
unset TL_KILL_SWITCH               2>/dev/null || true

# ── 2. Production config ──────────────────────────────────────────
export TRADE_LABS_MODE=PAPER
export TRADE_LABS_EXECUTION_BACKEND=IB
export TRADE_LABS_ARMED=1
export EXECUTION_ENABLED=true          # Enable real bracket order submission
export BUS_BACKEND=local               # In-process bus (no Redis needed)
export ALLOW_EXTENDED_HOURS=false      # RTH only for safety
export TL_EXEC_SIM_FRICTION=false      # Disable sim — we want real IB fills

# ── 3. Safety limits ─────────────────────────────────────────────
export TL_KS_DAILY_LOSS_PCT=0.02
export TL_KS_MAX_TRADES_HOUR=50
export TL_KS_MAX_PER_SYMBOL_HOUR=5
export TL_KS_MAX_LOSS_STREAK=5
export TL_RISK_MAX_TRADES_PER_DAY=500
export TL_SESSION_REPORT=1

# ── 4. Paper calibration (relax for discovery) ───────────────────
export TRADE_LABS_BASE_MIN_UNIFIED_SCORE=50
export TRADE_LABS_CONVICTION_MIN_UNIFIED_SCORE=48
export TRADE_LABS_BOUNCE_MIN_UNIFIED_SCORE=45
export TRADE_LABS_BOUNCE_MIN_SAMPLE_SIZE=0

# ── 5. Logging setup ─────────────────────────────────────────────
mkdir -p logs

# Clean up old logs
find logs/ -name "production_*.log" -mtime +${LOG_RETENTION_DAYS} -delete 2>/dev/null || true

# ── 6. Preflight: verify IB Gateway ─────────────────────────────
IB_PORT="${IB_PORT:-7497}"
if ! nc -z 127.0.0.1 "$IB_PORT" 2>/dev/null; then
    echo "❌  Cannot reach IB Gateway on 127.0.0.1:${IB_PORT}"
    echo "    Start TWS or IB Gateway in PAPER mode first."
    exit 1
fi
echo "✅  IB Gateway reachable on port ${IB_PORT}"

# ── 7. Auto-restart loop ─────────────────────────────────────────
export PYTHONPATH="$(pwd)"
TODAY=$(date +%Y%m%d)
RESTART_COUNT=0

echo "╔══════════════════════════════════════════════════════════╗"
echo "║   TRADE-LABS · Production Session (Mac Studio 24hr)      ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Mode       : PAPER                                      ║"
echo "║  Backend    : IB (real bracket orders)                    ║"
echo "║  Execution  : ENABLED                                     ║"
echo "║  Armed      : YES                                         ║"
echo "║  Bus        : LocalBus (in-process)                       ║"
echo "║  Forces     : ALL DISABLED                                ║"
echo "║  Kill SW    : 2% daily / 50 trades·hr / 5 loss streak    ║"
echo "║  Max trades : 500/day                                     ║"
echo "║  Restart    : up to ${MAX_RESTARTS}/day, ${RESTART_DELAY_S}s delay     ║"
echo "║  Log retain : ${LOG_RETENTION_DAYS} days                  ║"
echo "║                                                           ║"
echo "║  Press Ctrl+C to stop                                     ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

while true; do
    # Reset counter on new day
    CURRENT_DAY=$(date +%Y%m%d)
    if [[ "$CURRENT_DAY" != "$TODAY" ]]; then
        TODAY="$CURRENT_DAY"
        RESTART_COUNT=0
    fi

    if [[ "$RESTART_COUNT" -ge "$MAX_RESTARTS" ]]; then
        echo "❌  Max restarts ($MAX_RESTARTS) reached for today. Manual intervention required."
        echo "    Check logs in logs/ for the crash cause."
        exit 1
    fi

    SESSION_TS=$(date +%Y%m%d_%H%M%S)
    LOG_FILE="logs/production_${SESSION_TS}.log"

    echo "🚀  Starting session (restart #${RESTART_COUNT}) — logging to ${LOG_FILE}"

    # Run the arm system
    python -u -m src.arms.dev_all_in_one 2>&1 | tee "$LOG_FILE"
    EXIT_CODE=$?

    # If clean shutdown (Ctrl+C / SIGTERM), exit without restart
    if [[ "$EXIT_CODE" -eq 0 ]] || [[ "$EXIT_CODE" -eq 130 ]]; then
        echo "✅  Clean shutdown (exit code ${EXIT_CODE})"
        break
    fi

    RESTART_COUNT=$((RESTART_COUNT + 1))
    echo ""
    echo "⚠️   Process exited with code ${EXIT_CODE} — restart #${RESTART_COUNT} in ${RESTART_DELAY_S}s..."
    echo "    Log: ${LOG_FILE}"
    echo ""

    # Run health check on the crash log
    if [[ -f "$LOG_FILE" ]]; then
        echo "── Quick health check on crash log ──"
        grep -cE "Traceback|CRITICAL|Exception:" "$LOG_FILE" 2>/dev/null || true
        echo "── Last 5 error lines ──"
        grep -E "Traceback|ERROR|CRITICAL|Exception:" "$LOG_FILE" | tail -5 || true
        echo ""
    fi

    sleep "$RESTART_DELAY_S"

    # Re-check IB Gateway before restart
    if ! nc -z 127.0.0.1 "$IB_PORT" 2>/dev/null; then
        echo "❌  IB Gateway not reachable — waiting 60s before retry..."
        sleep 60
        if ! nc -z 127.0.0.1 "$IB_PORT" 2>/dev/null; then
            echo "❌  IB Gateway still down. Exiting."
            exit 1
        fi
    fi
done
