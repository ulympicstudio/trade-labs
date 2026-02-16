# Trade Labs System Architecture

## Complete System Overview

```
┌─────────────────────────────────────────────────────────────────────────┐
│                   Trade Labs Master Orchestrator                         │
│                    (trade_labs_orchestrator.py)                          │
└─────────────────────────────────────────────────────────────────────────┘
                                    │
                    ┌───────────────┼───────────────┐
                    │               │               │
                    ▼               ▼               ▼
            ┌──────────────┐  ┌──────────────┐  ┌──────────────┐
            │   Pipeline   │  │  Scheduler   │  │  Reporting & │
            │ Orchestrator │  │ (APScheduler)│  │     Recon    │
            └──────────────┘  └──────────────┘  └──────────────┘
                    │               │               │
                    ▼               ▼               ▼
        ┌─────────────────┐  ┌─────────────┐  ┌─────────────┐
        │ Full Pipeline   │  │Standard     │  │Report       │
        │(src/signals/    │  │Schedule:    │  │Generator    │
        │ run_full_...)   │  │- 9:30 AM    │  │ Position    │
        │                 │  │- 12:00 PM   │  │ Reconciler  │
        │ 1. Scanner      │  │- 4:00 PM    │  └─────────────┘
        │ 2. Scorer       │  │- 5:00 PM    │       │    │
        │ 3. Executor     │  │             │       ▼    ▼
        └─────────────────┘  └─────────────┘   ┌───────────────┐
                    │                          │Data Analysis  │
                    ▼                          │ + Validation  │
        ┌──────────────────┐                   └───────────────┘
        │ Pipeline Logger  │
        │ (Structured JSON)│
        └────────┬─────────┘
                 │
        ┌────────▼─────────┐
        │ Trade History DB │
        │   (JSON files)   │
        │                  │
        │ - runs.json      │
        │ - trades.json    │
        └──────────────────┘
```

## Data Flow Diagram

```
Interactive Brokers (IB)
         │
         ▼
    ┌─────────────────────────────────┐
    │  Data Module                    │
    │  (ib_market_data.py)           │
    │                                 │
    │ - Connect to IB                 │
    │ - Fetch scanner results         │
    │ - Get market prices             │
    │ - Historical bars for ATR       │
    └─────────────────────────────────┘
         │        │         │
         ▼        ▼         ▼
    ┌──────────┐ ┌────────┐ ┌──────────┐
    │ Signals  │ │ Broker │ │   Risk   │
    │ (scan)   │ │(scoring)│ │(position)│
    └──────────┘ └────────┘ └──────────┘
         │        │         │
         └────────┼─────────┘
                  │
                  ▼
        ┌──────────────────┐
        │ Signal Engine    │
        │ - Score & rank   │
        │ - Top N output   │
        └──────────────────┘
                  │
                  ▼
        ┌──────────────────────────┐
        │ Execution Pipeline       │
        │                          │
        │ 1. Size position         │
        │ 2. Apply risk guards     │
        │ 3. Place orders (SIM/IB) │
        └──────────────────────────┘
                  │
         ┌────────┴────────┐
         │                 │
         ▼                 ▼
    ┌──────────┐      ┌──────────┐
    │   SIM    │      │    IB    │
    │ Backend  │      │ Backend  │
    │(paper)   │      │(paper)   │
    └──────────┘      └──────────┘
         │                 │
         └────────┬────────┘
                  │
    ┌─────────────▼──────────────┐
    │  Order Results             │
    │  - Order IDs               │
    │  - Stop order IDs          │
    │  - Execution status        │
    └─────────────┬──────────────┘
                  │
    ┌─────────────▼──────────────┐
    │  Pipeline Logger           │
    │  - Event logging (JSON)    │
    │  - Run ID tracking         │
    │  - Full audit trail        │
    └─────────────┬──────────────┘
                  │
    ┌─────────────▼──────────────┐
    │  Trade History Database    │
    │  - Record trades           │
    │  - Calculate P&L           │
    │  - Query by symbol/status  │
    │  - Generate summaries      │
    └─────────────┬──────────────┘
                  │
        ┌─────────┼─────────┐
        │         │         │
        ▼         ▼         ▼
    ┌────────┐ ┌────────┐ ┌──────────┐
    │Reports │ │Analytics│ │Recon    │
    │.md/.csv│ │Summaries│ │Position │
    └────────┘ └────────┘ │Validation│
                          └──────────┘
```

## Component Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    ORCHESTRATION LAYER                       │
├─────────────────────────────────────────────────────────────┤
│ trade_labs_orchestrator.py                                  │
│ └─ CLI Interface (pipeline | report | reconcile | stats)   │
└─────────────────────────────────────────────────────────────┘
                              │
        ┌─────────────────────┼─────────────────────┐
        │                     │                     │
┌───────▼──────────┐  ┌───────▼──────────┐  ┌──────▼──────────┐
│   EXECUTION      │  │  SCHEDULING      │  │  OBSERVABILITY  │
├──────────────────┤  ├──────────────────┤  ├─────────────────┤
│ Signals  Pipeline│  │ Scheduler        │  │ Log Manager     │
│ ├─ Scanner      │  │ ├─ 9:30 AM scan  │  │ ├─ JSONFormatter │
│ ├─ Scorer       │  │ ├─ 12:00 PM scan │  │ ├─ PipelineLog. │
│ ├─ Executor     │  │ ├─ 4:00 PM recon │  │ └─ setup_log()  │
│ └─ Output       │  │ └─ 5:00 PM report│  │                 │
│                 │  │                   │  │ Report Gener.  │
│ Data Module:    │  │ APScheduler      │  │ ├─ Daily        │
│ ├─ IB connect   │  │ └─ Background    │  │ ├─ Weekly       │
│ ├─ Market data  │  │    jobs          │  │ ├─ Monthly      │
│ ├─ Contracts    │  │                   │  │ └─ Export       │
│ └─ History      │  │                   │  │                 │
│                 │  │                   │  │ Position Recon. │
│ Risk Engine:    │  │                   │  │ ├─ IB positions │
│ ├─ Position size│  │                   │  │ ├─ Expected pos │
│ ├─ Guards       │  │                   │  │ ├─ Discrepancy  │
│ └─ Validate     │  │                   │  │ └─ P&L calc    │
│                 │  │                   │  │                 │
│ Execution:      │  │                   │  │ Trade History   │
│ ├─ Place orders │  │                   │  │ ├─ Record runs  │
│ ├─ SIM mode     │  │                   │  │ ├─ Record trades│
│ ├─ IB mode      │  │                   │  │ ├─ Close trades │
│ └─ Results      │  │                   │  │ └─ Query stats  │
└──────────────────┘  └──────────────────┘  └─────────────────┘
```

## Data Storage Architecture

```
Trade Labs
│
├── src/
│   ├── signals/
│   │   ├── run_full_pipeline.py      [Orchestrator - runs scan→score→exec]
│   │   └── score_candidates.py       [Scorer - 0-100 ranking]
│   ├── execution/
│   │   └── pipeline.py               [Executor - place orders]
│   ├── data/
│   │   └── ib_market_data.py         [Data fetcher - IB connection]
│   ├── risk/
│   │   └── position_sizing.py        [Risk - calculate quantities]
│   └── utils/
│       ├── log_manager.py            [Logging - structured JSON]
│       ├── trade_history_db.py       [Persistence - trade records]
│       ├── report_generator.py       [Reporting - analytics]
│       ├── position_reconciler.py    [Reconciliation - validation]
│       └── scheduler.py              [Scheduling - APScheduler]
│
├── config/
│   ├── ib_config.py                  [IB connection settings]
│   ├── runtime.py                    [Runtime configuration]
│   └── identity.py                   [System identification]
│
├── data/
│   ├── trade_history/
│   │   ├── runs.json                 [Pipeline execution records]
│   │   └── trades.json               [All executed trades]
│   └── reports/
│       ├── report_YYYY-MM-DD.md      [Daily markdown reports]
│       ├── report_YYYY-MM-DD.csv     [Daily CSV exports]
│       └── position_reconciliation.json
│
├── logs/
│   └── pipeline/
│       ├── pipeline_orchestrator.log [JSON event logs]
│       └── trade_labs.log            [Main log file]
│
├── trade_labs_orchestrator.py        [Master CLI]
├── PHASE2_README.md                  [Feature documentation]
└── QUICK_REFERENCE.md                [Quick start guide]
```

## Execution Sequence

```
1. Pipeline Run Initiated
   │
   ├─ Logger: log_manager.PipelineLogger initialized with run_id
   ├─ Event: scan_started(run_id)
   │
   ├─ Scanner: Market scanner queries IB (MOST_ACTIVE)
   ├─ Logger: Event logged to JSON
   │
   ├─ Scorer: Score candidates (0-100 scale)
   ├─ Logger: Each score logged
   ├─ Selector: Top N candidates chosen
   │
   └─ Event: scan_completed(found_count)
      │
      ├─ For each candidate:
      │  ├─ Data: Fetch current price & ATR
      │  ├─ Executor: get_recent_price_from_history()
      │  │
      │  ├─ Event: execution_started(symbol)
      │  │
      │  ├─ Sizing: Position calculation (risk_percent of equity)
      │  ├─ Guards: Apply risk filters
      │  │
      │  ├─ Execution: Place order (SIM or IB)
      │  │  ├─ Buy order at market
      │  │  └─ Stop order at calculated stop loss
      │  │
      │  ├─ Results: Capture order IDs
      │  │
      │  ├─ HistoryDB: record_trade(
      │  │             run_id, symbol, entry_price,
      │  │             quantity, stop_loss, order_result)
      │  │
      │  └─ Event: execution_completed(symbol, ok, details)
      │     └─ Logged to JSON + stored
      │
      └─ Event: pipeline_completed(executed, successful)
         └─ HistoryDB: record_pipeline_run(...)

2. After Execution
   └─ HistoryDB writes to JSON files
      ├─ data/trade_history/runs.json (append)
      └─ data/trade_history/trades.json (append)

3. Reporting (scheduled)
   └─ ReportGenerator queries HistoryDB
      ├─ Filter by date
      ├─ Calculate metrics
      ├─ Generate markdown report
      ├─ Export CSV
      └─ Save to data/reports/

4. Reconciliation (scheduled)
   └─ PositionReconciler compares
      ├─ Fetch from IB: actual positions
      ├─ Fetch from HistoryDB: expected positions
      ├─ Compare quantities & prices
      ├─ Calculate unrealized P&L
      └─ Flag discrepancies

5. Scheduler Coordination
   └─ APScheduler runs jobs at configured times
      ├─ 9:30 AM: scan_pipeline(candidates=5)
      ├─ 12:00 PM: scan_pipeline(candidates=3)
      ├─ 4:00 PM: reconcile_positions()
      └─ 5:00 PM: generate_daily_report()
```

## Technology Stack

```
┌──────────────────────────────────────────────────┐
│           COMMUNICATION                          │
│  - Interactive Brokers API (ib_insync)          │
│  - WebSocket connection to IB                    │
└──────────────────────────────────────────────────┘
         │
┌────────▼───────────────────────────────────────┐
│           CORE LOGIC                            │
│  - Python 3.13                                 │
│  - Dataclasses (type safety)                   │
│  - Built-in libraries                          │
└────────────────────────────────────────────────┘
         │
┌────────▼───────────────────────────────────────┐
│           DATA PERSISTENCE                      │
│  - JSON files (simple, portable)               │
│  - Local filesystem storage                    │
│  - CSV export support                          │
└────────────────────────────────────────────────┘
         │
┌────────▼───────────────────────────────────────┐
│           SCHEDULING                            │
│  - APScheduler (background job runner)         │
│  - Cron-like triggers                          │
│  - Timezone support (US/Eastern)               │
└────────────────────────────────────────────────┘
         │
┌────────▼───────────────────────────────────────┐
│           EXECUTION MODES                       │
│  - SIM: Simulated order placement              │
│  - IB: Real paper trading (IB account)         │
│  - PAPER: Safe mode (default)                  │
└────────────────────────────────────────────────┘
```

## Safety Mechanisms

```
┌────────────────────────────────────────────────────┐
│            EXECUTION SAFETY GATES                   │
├────────────────────────────────────────────────────┤
│                                                    │
│  1. Paper Mode (Default)                          │
│     └─ TRADE_LABS_MODE=PAPER enforced           │
│                                                    │
│  2. Arm Flag Check                                │
│     └─ TRADE_LABS_ARMED=0 blocks real orders   │
│                                                    │
│  3. Risk Guards                                   │
│     ├─ Position size limits                       │
│     ├─ Daily loss limits                          │
│     ├─ Open risk calculations                     │
│     └─ Stop loss enforcement                      │
│                                                    │
│  4. Position Reconciliation                       │
│     └─ Detects unexpected positions              │
│                                                    │
│  5. Audit Trail                                   │
│     └─ Every action logged with timestamp        │
│                                                    │
│  6. Manual Override                               │
│     └─ Each trade can be examined before exec    │
│                                                    │
└────────────────────────────────────────────────────┘
```

## Monitoring & Observability

```
Real-time Visibility:
└─ Console output (trades, prices, results)

JSON Event Logging:
└─ logs/pipeline/*.log
   ├─ Timestamp, level, module, line number
   ├─ Message content
   ├─ Exception tracebacks
   └─ Custom context fields

Trade History:
└─ data/trade_history/
   ├─ runs.json (pipeline execution records)
   └─ trades.json (individual trades)

Reports:
└─ data/reports/
   ├─ Markdown (human-readable)
   └─ CSV (machine-readable, Excel-friendly)

Statistics:
└─ Real-time aggregation
   ├─ Win rate
   ├─ Total PnL
   ├─ Average metrics
   └─ Trending analysis
```

---

## Key Improvements from Phase 1 → Phase 2

| Aspect | Phase 1 | Phase 2 |
|--------|---------|---------|
| Automation | Pipeline execution | Full orchestration + scheduling |
| Logging | Print statements | Structured JSON + events |
| History | None | JSON database with queries |
| Reporting | None | Daily/weekly/monthly analytics |
| Validation | Manual | Automatic reconciliation |
| Scheduling | None | APScheduler with market hours |
| Observability | Limited | Complete audit trail |
| Scalability | Single run | 24/7 automated operation |

---

## System Health Checks

```
✓ IB Connection
  └─ connect_ib() → ticker validation

✓ Trade Persistence
  └─ record_trade() → verify JSON write

✓ Reporting
  └─ generate_daily_report() → P&L calculation

✓ Reconciliation
  └─ reconcile() → position comparison

✓ Scheduler
  └─ scheduler.start() → job execution

✓ Logging
  └─ PipelineLogger → event tracking
```

---

This architecture supports:
- ✅ Fully automated 24/7 operation
- ✅ Complete audit trail for compliance
- ✅ Real-time monitoring and alerting
- ✅ Performance analysis and optimization
- ✅ Risk management and validation
- ✅ Future scaling to databases or cloud
