#!/usr/bin/env bash
set -euo pipefail

# How to run OFFHOURS_PAPER_TEST:
#   bash scripts/run_offhours_paper_test.sh

cd "$(dirname "$0")/.."

# OFFHOURS_PAPER_TEST harness: explicit paper + sim + AH test mode
export TRADE_LABS_MODE=PAPER
export TRADE_LABS_EXECUTION_BACKEND=SIM
export UTS_PAPER_AH_TEST=1
export TRADE_LABS_ARMED=0
export TL_LIVE_TRADING=0
export EXECUTION_ENABLED=false
export OFFHOURS_PAPER_TEST=1

SESSION_DATE=$(date +%Y%m%d_%H%M%S)
LOG_FILE="logs/offhours_paper_test_${SESSION_DATE}.log"
mkdir -p logs

cat <<EOF
Running OFFHOURS_PAPER_TEST session
  mode=${TRADE_LABS_MODE} backend=${TRADE_LABS_EXECUTION_BACKEND} paper_ah_test=${UTS_PAPER_AH_TEST}
  armed=${TRADE_LABS_ARMED} execution_enabled=${EXECUTION_ENABLED} live_trading=${TL_LIVE_TRADING}
  log=${LOG_FILE}
EOF

export PYTHONPATH="$(pwd)"
python -u scripts/offhours_paper_test_sanity.py | tee -a "$LOG_FILE"
echo "OFFHOURS_PAPER_TEST starting live loop" | tee -a "$LOG_FILE"
python -u -m src.live_loop_10s 2>&1 | tee -a "$LOG_FILE"
# FUNNELPROBE counters are in-process only and are emitted to the log by the
# live loop at runtime (search for FUNNEL_PROBE in the log above).
# For durable post-run validation use the JSONL-backed CLIs:
#   python -m src.monitoring.funnel_reconcile_cli
#   python -m src.monitoring.reject_event_cli --report
