# Trade Labs Phase 2: Observability & Persistence

**Status**: ✅ COMPLETE

Phase 2 builds comprehensive observability and persistence infrastructure on top of the Phase 1 automated pipeline.

## What's New

### 1. Structured Logging Infrastructure (`src/utils/log_manager.py`)
- **JSONFormatter**: Formats all logs as structured JSON for easy parsing and analysis
- **setup_logging()**: Configures console + file handlers with separate log levels
- **PipelineLogger**: High-level event tracking for pipeline execution
- **Features**:
  - Events: `scan_started`, `candidate_scored`, `execution_completed`, `pipeline_completed`
  - Full audit trail in JSON format
  - Module-level logger creation with `get_logger(__name__)`

### 2. Trade History Persistence (`src/utils/trade_history_db.py`)
- **TradeHistoryDB**: JSON-based local database for trade tracking
- **Key Methods**:
  - `record_pipeline_run()`: Log each trading session
  - `record_trade()`: Store executed trades with details
  - `close_trade()`: Mark position as closed, calculate P&L
  - `get_trade_history()`: Query trades by symbol/status
  - `get_daily_summary()`: PnL summary for any date
  - `get_stats()`: Overall trading statistics
- **Data Stored** (per trade):
  - Entry/exit price, quantity, stop loss
  - Order IDs, execution status
  - Realized P&L and percentage
  - Timestamps for full audit trail

### 3. Daily Report Generator (`src/utils/report_generator.py`)
- **ReportGenerator**: Creates multi-format performance reports
- **Report Types**:
  - Daily: Per-day performance summary
  - Weekly: Week-to-date stats
  - Monthly: Month-to-date stats
- **Output Formats**:
  - Markdown reports (human-readable)
  - CSV exports (for Excel/analysis)
- **Key Metrics**:
  - Total PnL and win rate
  - Average win/loss
  - Largest win/loss
  - Profit factor
  - Trade duration analytics

### 4. Position Reconciliation (`src/utils/position_reconciler.py`)
- **PositionReconciler**: Syncs trade records vs IB reality
- **Validates**:
  - Actual IB positions vs expected positions from trade history
  - Identifies quantity mismatches (critical for risk management)
  - Flags positions not in trade history
  - Calculates unrealized P&L per position
- **Output**:
  - Status: OK / DISCREPANCY / MISMATCH
  - Detailed mismatch report with recommended actions
  - Exportable as JSON for debugging

### 5. Scheduled Operations (`src/utils/scheduler.py`)
- **PipelineScheduler**: Background task scheduler using APScheduler
- **Standard Schedule** (can run 24/7, trades only M-F 9:30-16:00 ET):
  - **9:30 AM**: Market open scan (5 candidates)
  - **12:00 PM**: Mid-day scan (3 candidates)
  - **4:00 PM**: Position reconciliation
  - **5:00 PM**: Daily report generation
- **Features**:
  - US/Eastern timezone support
  - Customizable schedules
  - Start/stop/pause/resume controls
  - Job status tracking

### 6. Master Orchestrator (`trade_labs_orchestrator.py`)
- **TradeLabsOrchestrator**: Central command for operating entire system
- **Modes**:
  - `pipeline`: Run scanning + scoring + execution
  - `reconcile`: Check positions against IB
  - `report`: Generate daily performance report
  - `stats`: Display overall trading statistics
  - `scheduler`: Run automated scheduled operations
- **Command Line Usage**:
  ```bash
  # Run pipeline now (5 candidates)
  python trade_labs_orchestrator.py --mode pipeline --candidates 5
  
  # Generate report for specific date
  python trade_labs_orchestrator.py --mode report --date 2025-02-14
  
  # Start scheduler for 24/7 automated operation
  python trade_labs_orchestrator.py --mode scheduler
  
  # Show overall trading stats
  python trade_labs_orchestrator.py --mode stats
  
  # Test mode (SPY only)
  python trade_labs_orchestrator.py --mode pipeline --spy-only
  ```

## Integration with Phase 1

Phase 2 instruments Phase 1 components:

| Component | Integration |
|-----------|------------|
| `run_full_pipeline.py` | Logs every event, records trades to history |
| `log_manager.py` | Console + file logging with structured JSON |
| `trade_history_db.py` | Stores all executed trades with P&L |
| `report_generator.py` | Analyzes trade history for reporting |
| `position_reconciler.py` | Validates against live positions |
| `scheduler.py` | Runs pipeline and reconciliation on schedule |

## Data Flow

```
Pipeline Execution (Phase 1)
    ↓
PipelineLogger events → Structured JSON logs
    ↓
TradeHistoryDB records → JSON files in data/trade_history/
    ↓
ReportGenerator parses → CSV + Markdown reports
    ↓
PositionReconciler validates → Reconciliation report
    ↓
Scheduler coordinates → 24/7 automated operation
```

## Storage Structure

```
data/
├── trade_history/
│   ├── runs.json          # Pipeline execution records
│   └── trades.json        # All executed trades with P&L
├── reports/
│   ├── report_YYYY-MM-DD.md     # Daily markdown reports
│   ├── report_YYYY-MM-DD.csv    # Daily CSV data
│   └── position_reconciliation.json  # Position reconciliation data
logs/
└── pipeline/
    ├── pipeline_orchestrator.log  # Structured logs (JSON format)
    └── trade_labs.log             # Main log file
```

## Key Capabilities

### Real-Time Visibility
- Every pipeline run logged with run_id for tracing
- Every executed trade recorded with entry/exit prices
- Every event tracked for audit trail compliance

### Performance Analysis
- Win rate and profit factor calculation
- Daily/weekly/monthly PnL summaries
- Average trade metrics
- Trade duration analytics

### Risk Management
- Position reconciliation catches discrepancies
- Stop loss tracking per position
- Unrealized P&L calculation
- Quantity mismatch detection

### Operational Automation
- Market open/mid-day/close scheduling
- Automatic reconciliation after market
- Daily report generation
- Stats tracking and trending

## Example Usage

### Run Pipeline Manually
```bash
python trade_labs_orchestrator.py --mode pipeline
```

### Generate Report
```bash
python trade_labs_orchestrator.py --mode report --date 2025-02-14
```

### Reconcile Positions
```bash
python trade_labs_orchestrator.py --mode reconcile
```

### View Stats
```bash
python trade_labs_orchestrator.py --mode stats
```

### Run 24/7 Scheduler
```bash
python trade_labs_orchestrator.py --mode scheduler
```

## Testing

Test scripts included for validation:

- `test_trade_history.py`: Verify trade persistence
- `test_report_generator.py`: Verify report generation

Run tests:
```bash
python test_trade_history.py
python test_report_generator.py
```

## Future Enhancements

Phase 3 candidates:
- Database migration (SQLite/PostgreSQL for larger scale)
- Real-time dashboard (Streamlit/FastAPI)
- Advanced analytics (drawdown, Sharpe ratio, etc.)
- Email/Slack notifications
- Machine learning signal optimization
- Position hedging strategies
- Portfolio rebalancing engine

## Notes

- All times are in US/Eastern (market timezone)
- Trade history stored as JSON for simplicity (can migrate to DB)
- Reconciliation compares expected vs actual positions
- All operations are PAPER/LIVE-safe (never trades live without explicit arm)
- Logging includes full context for debugging

---

**Status**: Phase 2 Complete ✅  
**Next**: Deploy scheduler for production use or proceed to Phase 3 enhancements
