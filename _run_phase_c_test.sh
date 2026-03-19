#!/usr/bin/env bash
# Phase C baseline test launcher
set -euo pipefail

export TL_TEST_FORCE_SESSION=RTH
export TL_TEST_FORCE_SECTOR_SCORE=80
export SIGNAL_FORCE_INTENT=true
export TL_TEST_FORCE_INTENT_INTERVAL=15
export TL_ATTRIB_ENABLED=1
export TL_ATTRIB_MONITOR_ENABLED=1
export TL_TUNING_ENABLED=1
export TL_TUNING_MONITOR_ENABLED=1
export TL_TUNING_FORCE_BUCKET=news
export TL_TUNING_FORCE_EDGE=positive
export TL_TUNING_FORCE_SAMPLE=20
export TL_EXEC_FORCE_PAPER_FILL=true
export TL_EXEC_SIM_FRICTION=true
export TL_DEV_HARNESS_INTERVAL_S=15
export TL_EXIT_FORCE_ACTION=TRIM_25
export TL_SCORECARD_MIN_TRADES=3
export BUS_BACKEND=local
export TL_PAPER=true
export TL_LOG_LEVEL=INFO

cd /Users/umronalkotob/trade-labs
exec python -u -m src.arms.dev_all_in_one
