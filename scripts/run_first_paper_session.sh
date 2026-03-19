#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────
# U.T.S. — First IB Paper Trading Session
#
# Paper-safe startup profile for the first real IB paper session.
# All force-path test flags OFF.  Conservative limits.  RTH only.
#
# Usage:
#   bash scripts/run_first_paper_session.sh
#
# Logs: logs/paper_session_<date>.log
# Stop: Ctrl+C  (graceful shutdown with summary)
# ──────────────────────────────────────────────────────────────────────
set -euo pipefail

DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$DIR"

DATE_TAG="$(date '+%Y%m%d_%H%M%S')"
LOG_FILE="logs/paper_session_${DATE_TAG}.log"
mkdir -p logs

# ══════════════════════════════════════════════════════════════════════
# SECTION 1: Trade mode — PAPER only
# ══════════════════════════════════════════════════════════════════════
export TL_TRADE_MODE=PAPER
export TRADE_LABS_MODE=PAPER
export TRADE_LABS_EXECUTION_BACKEND=SIM
export TRADE_LABS_ARMED=0

# Execution: disabled (SIM fills via sim-friction only, no live orders)
export EXECUTION_ENABLED=false
export TL_EXEC_SIM_FRICTION=true
export TL_EXEC_SIM_DELAY_MS=250
export TL_EXEC_SIM_SLIPPAGE_BPS=3

# ══════════════════════════════════════════════════════════════════════
# SECTION 2: Force-path test flags — ALL OFF
# ══════════════════════════════════════════════════════════════════════
unset TL_TEST_FORCE_SESSION 2>/dev/null || true
unset FORCE_SESSION 2>/dev/null || true
unset SIGNAL_FORCE_INTENT 2>/dev/null || true
unset TL_TEST_FORCE_INTENT_INTERVAL 2>/dev/null || true
unset TL_EXEC_FORCE_PAPER_FILL 2>/dev/null || true
unset TL_EXIT_FORCE_ACTION 2>/dev/null || true
unset TL_EXIT_FORCE_MFE 2>/dev/null || true
unset TL_EXIT_FORCE_MAE 2>/dev/null || true
unset TL_EXIT_FORCE_PNL 2>/dev/null || true
unset TL_EXIT_FORCE_PLAYBOOK 2>/dev/null || true
unset TL_EXIT_FORCE_MODE 2>/dev/null || true
unset TL_CERT_MODE 2>/dev/null || true
unset TL_TUNING_FORCE_BUCKET 2>/dev/null || true
unset TL_TUNING_FORCE_EDGE 2>/dev/null || true
unset TL_TUNING_FORCE_SAMPLE 2>/dev/null || true
unset TL_TEST_FORCE_SECTOR_SCORE 2>/dev/null || true

# ══════════════════════════════════════════════════════════════════════
# SECTION 3: Conservative risk limits
# ══════════════════════════════════════════════════════════════════════
export TL_ACCOUNT_EQUITY=100000
export MAX_RISK_USD_PER_TRADE=50
export TL_RISK_PER_TRADE_PCT=0.005

# Heat cap: enabled, max 3 concurrent positions for first session
export TL_HEAT_CAP_ENABLED=true
export TL_HEAT_MAX_OPEN_POS=3
export TL_HEAT_MAX_TOTAL_RISK_PCT=0.015

# Kill switch: conservative thresholds
export TL_KS_DAILY_LOSS_PCT=0.02
export TL_KS_MAX_TRADES_HOUR=20
export TL_KS_MAX_PER_SYMBOL_HOUR=3
export TL_KS_MAX_LOSS_STREAK=4

# No extended hours
export ALLOW_EXTENDED_HOURS=false

# ══════════════════════════════════════════════════════════════════════
# SECTION 4: Subsystems — all enabled for full visibility
# ══════════════════════════════════════════════════════════════════════
export TL_ATTRIB_ENABLED=1
export TL_ATTRIB_MONITOR_ENABLED=1
export TL_SCORECARD_ENABLED=1
export TL_SCORECARD_MONITOR_ENABLED=1
export TL_TUNING_ENABLED=1
export TL_TUNING_MONITOR_ENABLED=1
export TL_EXIT_MONITOR_ENABLED=1

# ══════════════════════════════════════════════════════════════════════
# SECTION 5: Bus + logging
# ══════════════════════════════════════════════════════════════════════
export BUS_BACKEND=local
export TL_LOG_LEVEL=INFO
export TL_FIRST_PAPER_SESSION=true

# ══════════════════════════════════════════════════════════════════════
# LAUNCH
# ══════════════════════════════════════════════════════════════════════
echo "╔═══════════════════════════════════════════════════════════╗"
echo "║       U.T.S. — First IB Paper Trading Session            ║"
echo "╠═══════════════════════════════════════════════════════════╣"
echo "║  Mode           : PAPER (SIM backend, no live orders)    ║"
echo "║  Max positions  : 3                                      ║"
echo "║  Risk/trade     : \$50 (0.5%)                             ║"
echo "║  Session        : RTH only                               ║"
echo "║  Force flags    : ALL OFF                                ║"
echo "║  Log            : ${LOG_FILE}  ║"
echo "║                                                           ║"
echo "║  Press Ctrl+C for graceful shutdown with summary          ║"
echo "╚═══════════════════════════════════════════════════════════╝"
echo ""

exec python -u -m src.arms.dev_all_in_one 2>&1 | tee "$LOG_FILE"
