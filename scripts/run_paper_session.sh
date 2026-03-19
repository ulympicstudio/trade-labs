#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
#  TRADE-LABS · Real Paper Session Launcher
#
#  Runs live_loop_10s.py against IB paper account with ALL force/test
#  flags disabled.  Natural system behavior only.
#
#  Prerequisites:
#    • TWS or IB Gateway running in PAPER mode on port 7497
#    • .env file configured with FINNHUB_API_KEY
#
#  Usage:
#    ./scripts/run_paper_session.sh
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

cd "$(dirname "$0")/.."

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

# Kill switch force (do NOT force-kill on startup)
unset TRADE_LABS_FORCE_KILL        2>/dev/null || true
unset TL_KILL_SWITCH               2>/dev/null || true

# ── 2. Paper-safe operational config ───────────────────────────────
export TRADE_LABS_MODE=PAPER
export TRADE_LABS_EXECUTION_BACKEND=IB
export TRADE_LABS_ARMED=1

# ── 3. Safety limits ──────────────────────────────────────────────
# Kill switch thresholds (conservative for first paper session)
export TL_KS_DAILY_LOSS_PCT=0.02          # 2% daily loss cap
export TL_KS_MAX_TRADES_HOUR=10           # max 10 trades/hour
export TL_KS_MAX_PER_SYMBOL_HOUR=2        # max 2 per symbol/hour
export TL_KS_MAX_LOSS_STREAK=3            # halt after 3 consecutive losses

# ── 4. Session report output ──────────────────────────────────────
export TL_SESSION_REPORT=1

# ── 4b. Paper calibration profile ─────────────────────────────────
# Relax the narrowest bottlenecks for paper discovery.
# Kill switch, risk guards, and paper mode are all still active.
# Remove this block when you return to production thresholds.
export TRADE_LABS_BASE_MIN_UNIFIED_SCORE=50     # prod=70; paper needs room for catalyst_score (~50 center)
export TRADE_LABS_CONVICTION_MIN_UNIFIED_SCORE=48 # prod=68
export TRADE_LABS_BOUNCE_MIN_UNIFIED_SCORE=45     # prod=68; bounce is discovery mode
export TRADE_LABS_BOUNCE_MIN_SAMPLE_SIZE=0         # prod=12; fresh system has zero history

# ── 5. Logging ────────────────────────────────────────────────────
SESSION_DATE=$(date +%Y%m%d_%H%M%S)
LOG_FILE="logs/paper_session_live_${SESSION_DATE}.log"
mkdir -p logs

echo "╔══════════════════════════════════════════════════════════╗"
echo "║      TRADE-LABS · Real Paper Session (live_loop_10s)     ║"
echo "╠══════════════════════════════════════════════════════════╣"
echo "║  Mode     : PAPER                                       ║"
echo "║  Backend  : IB (real paper orders)                       ║"
echo "║  Armed    : YES                                          ║"
echo "║  Forces   : ALL DISABLED                                 ║"
echo "║  Calibr.  : PAPER (relaxed unified/bounce thresholds)    ║"
echo "║  Kill SW  : 2% daily / 10 trades·hr / 3 loss streak     ║"
echo "║  Log      : ${LOG_FILE}                                  ║"
echo "║                                                          ║"
echo "║  Press Ctrl+C to stop and generate session report        ║"
echo "╚══════════════════════════════════════════════════════════╝"
echo ""

# ── 6. Preflight: verify IB Gateway is reachable ──────────────────
IB_PORT="${IB_PORT:-7497}"
if ! nc -z 127.0.0.1 "$IB_PORT" 2>/dev/null; then
    echo "❌  Cannot reach IB Gateway on 127.0.0.1:${IB_PORT}"
    echo "    Start TWS or IB Gateway in PAPER mode first."
    exit 1
fi
echo "✅  IB Gateway reachable on port ${IB_PORT}"
echo ""

# ── 7. Launch ─────────────────────────────────────────────────────
export PYTHONPATH="$(pwd)"
python -u -m src.live_loop_10s 2>&1 | tee "$LOG_FILE"
