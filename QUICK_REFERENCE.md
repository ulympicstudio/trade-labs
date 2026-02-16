"""
Trade Labs Quick Reference Guide

Common operations and commands for operating the trading system.
"""

# ============================================================================
# QUICK START
# ============================================================================

# Run the full pipeline once (scan → score → execute)
python trade_labs_orchestrator.py --mode pipeline --candidates 5

# Start the 24/7 scheduler (automated trading)
python trade_labs_orchestrator.py --mode scheduler

# Generate today's report
python trade_labs_orchestrator.py --mode report

# Check positions against IB
python trade_labs_orchestrator.py --mode reconcile

# View trading statistics
python trade_labs_orchestrator.py --mode stats


# ============================================================================
# PIPELINE EXECUTION
# ============================================================================

# Run with default settings (5 candidates)
python trade_labs_orchestrator.py --mode pipeline

# Run with 10 candidates
python trade_labs_orchestrator.py --mode pipeline --candidates 10

# Test mode: trade SPY only
python trade_labs_orchestrator.py --mode pipeline --spy-only

# Run pipeline with 3 candidates
python trade_labs_orchestrator.py --mode pipeline --candidates 3


# ============================================================================
# REPORTING
# ============================================================================

# Generate report for today
python trade_labs_orchestrator.py --mode report

# Generate report for specific date
python trade_labs_orchestrator.py --mode report --date 2025-02-14

# View report in markdown
cat data/reports/report_2025-02-14.md

# Export trades to CSV
ls data/reports/*.csv


# ============================================================================
# WORKING WITH TRADE HISTORY (Python)
# ============================================================================

from src.utils.trade_history_db import TradeHistoryDB

db = TradeHistoryDB()

# Get all trades
trades = db.get_trade_history()

# Get trades for specific symbol
aapl_trades = db.get_trade_history(symbol="AAPL")

# Get only open trades
open_trades = db.get_trade_history(status="OPEN")

# Get daily summary
today_summary = db.get_daily_summary()  # Uses today
march_summary = db.get_daily_summary("2025-03-14")

# Get overall statistics
stats = db.get_stats()
print(f"Win Rate: {stats['win_rate']}%")
print(f"Total PnL: ${stats['total_pnl']}")

# Record a new pipeline run (manual)
db.record_pipeline_run(
    run_id="manual_run_001",
    backend="SIM",
    armed=False,
    num_candidates_scanned=10,
    num_candidates_executed=5,
    num_successful=3,
    details={"test": True}
)


# ============================================================================
# GENERATING REPORTS (Python)
# ============================================================================

from src.utils.report_generator import ReportGenerator

reporter = ReportGenerator()

# Generate daily report
daily = reporter.generate_daily_report("2025-02-14")
reporter.display_report(daily)

# Generate weekly report
weekly = reporter.generate_weekly_report("2025-02-10")  # Week starting 2/10
reporter.display_report(weekly)

# Generate monthly report
monthly = reporter.generate_monthly_report("2025-02")  # February 2025
reporter.display_report(monthly)

# Save as markdown
reporter.save_report_markdown(daily, "my_report.md")

# Save as CSV
reporter.save_report_csv(daily, "my_trades.csv")


# ============================================================================
# POSITION RECONCILIATION (Python)
# ============================================================================

from src.utils.position_reconciler import PositionReconciler
from src.data.ib_market_data import connect_ib

# Connect to IB
ib = connect_ib()

# Reconcile positions
reconciler = PositionReconciler()
result = reconciler.reconcile(ib)

# Display reconciliation
reconciler.display_reconciliation(result)

# Export to JSON
reconciler.export_reconciliation_json(result, "my_reconciliation.json")

# Get unrealized P&L
total_unrealized = reconciler.calculate_total_unrealized_pnl(result)
print(f"Total Unrealized P&L: ${total_unrealized}")

ib.disconnect()


# ============================================================================
# SCHEDULER OPERATIONS (Python)
# ============================================================================

from src.utils.scheduler import PipelineScheduler, create_standard_schedule
from src.signals.run_full_pipeline import run_full_pipeline

# Create standard schedule
scheduler = create_standard_schedule(
    pipeline_fn=run_full_pipeline,
    # reconciliation_fn=reconcile_positions,
    # report_fn=generate_daily_report,
)

# Start scheduler
scheduler.start()

# Pause scheduler
scheduler.pause()

# Resume scheduler
scheduler.resume()

# Stop scheduler
scheduler.stop()

# Get status
status = scheduler.get_status()
print(f"Running: {status['running']}")
print(f"Jobs: {status['jobs_count']}")


# ============================================================================
# MASTER ORCHESTRATOR (Python)
# ============================================================================

from trade_labs_orchestrator import TradeLabsOrchestrator

# Create orchestrator
orch = TradeLabsOrchestrator()

# Run pipeline
result = orch.run_pipeline(num_candidates=5)

# Generate report
report = orch.generate_daily_report()

# Reconcile positions
reconciliation = orch.reconcile_positions()

# Get stats
stats = orch.get_trading_stats()
orch.display_stats()

# Create and start scheduler
scheduler = orch.create_scheduler()
orch.start_scheduler()


# ============================================================================
# VIEWING LOGS
# ============================================================================

# View main log file
tail -f logs/pipeline/trade_labs.log

# View JSON logs (structured)
cat logs/pipeline/pipeline_orchestrator.log | python -m json.tool

# Search logs for specific symbol
grep "AAPL" logs/pipeline/*.log

# View last 20 lines of log
tail -20 logs/pipeline/trade_labs.log


# ============================================================================
# DATABASE INSPECTION
# ============================================================================

# View all runs
cat data/trade_history/runs.json | python -m json.tool

# View all trades
cat data/trade_history/trades.json | python -m json.tool

# Count trades
cat data/trade_history/trades.json | python -c "import sys,json; print(len(json.load(sys.stdin)))"

# View specific symbol's trades
cat data/trade_history/trades.json | python -c "import sys,json; trades=[t for t in json.load(sys.stdin) if t['symbol']=='AAPL']; print(json.dumps(trades, indent=2))"


# ============================================================================
# CONFIGURATION
# ============================================================================

# Set environment for SIM mode (default)
export TRADE_LABS_EXECUTION_BACKEND=SIM

# Set environment for IB mode (paper)
export TRADE_LABS_EXECUTION_BACKEND=IB

# Enable real orders (DANGEROUS - use with caution)
export TRADE_LABS_ARMED=1

# Disable real orders (safe - default)
export TRADE_LABS_ARMED=0

# Check current configuration
echo $TRADE_LABS_EXECUTION_BACKEND
echo $TRADE_LABS_ARMED


# ============================================================================
# TROUBLESHOOTING
# ============================================================================

# Check IB connection
from src.data.ib_market_data import connect_ib
ib = connect_ib()
print(ib.isConnected())
ib.disconnect()

# Test trade persistence
python test_trade_history.py

# Test report generation
python test_report_generator.py

# Check if APScheduler is installed
python -c "import apscheduler; print(apscheduler.__version__)"

# Install APScheduler if needed
pip install apscheduler

# View Python environment
conda info

# List all conda environments
conda env list

# Activate trade-labs environment
conda activate trade-labs


# ============================================================================
# COMMON PATTERNS
# ============================================================================

# Pattern 1: Run pipeline and immediately generate report
python trade_labs_orchestrator.py --mode pipeline --candidates 5
python trade_labs_orchestrator.py --mode report

# Pattern 2: Daily reconciliation and reporting
python trade_labs_orchestrator.py --mode reconcile
python trade_labs_orchestrator.py --mode report

# Pattern 3: Check overall health
python trade_labs_orchestrator.py --mode stats
python trade_labs_orchestrator.py --mode reconcile

# Pattern 4: Full end-to-end operation
python trade_labs_orchestrator.py --mode pipeline
python trade_labs_orchestrator.py --mode reconcile  
python trade_labs_orchestrator.py --mode report

# Pattern 5: Weekly analysis (run Friday evening)
python trade_labs_orchestrator.py --mode report --date <week_start_date>
# Then analyze data/reports/


# ============================================================================
# DATA ANALYSIS (Python/Pandas)
# ============================================================================

import pandas as pd
import json

# Load trade history
with open('data/trade_history/trades.json') as f:
    trades = json.load(f)

# Convert to DataFrame
df = pd.DataFrame(trades)

# Filter closed trades
closed = df[df['status'] == 'CLOSED']

# Group by symbol
by_symbol = closed.groupby('symbol')['pnl'].agg(['count', 'sum', 'mean'])
print(by_symbol)

# Calculate win rate
wins = (closed['pnl'] > 0).sum()
win_rate = wins / len(closed) * 100
print(f"Win Rate: {win_rate:.2f}%")

# Export to Excel
df.to_csv('all_trades.csv', index=False)


# ============================================================================
# PERFORMANCE MONITORING
# ============================================================================

# Check how often scheduler runs pipeline
cat logs/pipeline/*.log | grep "scan_started" | wc -l

# View execution times
cat logs/pipeline/*.log | grep -E "scan_started|pipeline_completed" | tail -20

# Count successful vs failed executions
cat logs/pipeline/*.log | grep "execution_completed" | python -c "
import sys, json
data = [json.loads(line.split('execution_completed] ')[1]) for line in sys.stdin if 'execution_completed' in line]
success = sum(1 for d in data if d.get('data', {}).get('success'))
total = len(data)
print(f'{success}/{total} successful')
"


# ============================================================================
# ADVANCED: CUSTOM SCHEDULING
# ============================================================================

from src.utils.scheduler import PipelineScheduler
from src.signals.run_full_pipeline import run_full_pipeline

scheduler = PipelineScheduler()

# Run at 10:00 AM
scheduler.schedule_custom(
    fn=run_full_pipeline,
    hour=10,
    minute=0,
    name="morning_scan",
    kwargs={"num_candidates": 5}
)

# Run at 2:00 PM
scheduler.schedule_custom(
    fn=run_full_pipeline,
    hour=14,
    minute=0,
    name="afternoon_scan",
    kwargs={"num_candidates": 3}
)

scheduler.start()


# ============================================================================
# REFERENCE: FILE LOCATIONS
# ============================================================================

Trade history:       data/trade_history/
├── runs.json        Pipeline execution records
└── trades.json      All trades with P&L

Reports:             data/reports/
├── *.md             Markdown reports (human-readable)
├── *.csv            CSV exports
└── *.json           Reconciliation data

Logs:                logs/pipeline/
├── *.log            JSON-formatted logs
└── *.log            Text logs

Source code:         src/
├── signals/         Scanner and scoring
├── execution/       Order execution
├── risk/            Risk management
├── indicators/      Technical analysis
└── utils/           Logging, reporting, reconciliation


# ============================================================================
# REFERENCE: KEY MODULES
# ============================================================================

run_full_pipeline.py           Main pipeline execution
trade_labs_orchestrator.py     Master control
src/utils/log_manager.py       Logging infrastructure
src/utils/trade_history_db.py  Trade persistence
src/utils/report_generator.py  Report generation
src/utils/position_reconciler.py Position validation
src/utils/scheduler.py         Job scheduling


# ============================================================================
# QUICK LINKS
# ============================================================================

Documentation:
- PHASE2_README.md        Phase 2 features and overview
- SESSION_SUMMARY.md      Complete session overview
- QUICK_REFERENCE.md      This file

Configuration:
- config/identity.py      System identification
- config/ib_config.py     IB connection settings
- config/runtime.py       Runtime configuration

Main Entry Points:
- trade_labs_orchestrator.py   CLI interface
- src/signals/run_full_pipeline.py  Pipeline execution
- test_trade_history.py   Persistence test
- test_report_generator.py Report test


===========================================================================================
For more details, see:
- PHASE2_README.md for feature documentation
- SESSION_SUMMARY.md for overview
- Individual module docstrings for API details
===========================================================================================
"""
