# U.T.S. Architecture Document

**Date:** 2026-03-16
**Codebase:** `/Users/umronalkotob/trade-labs/`
**Python:** 3.13 | **Venv:** `.venv/`

---

## A. High-Level System Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                        OPERATOR (Ulympic)                           │
│                   Manual review / override / monitoring              │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     ENTRYPOINTS                                     │
│                                                                     │
│   src/arms/dev_all_in_one.py  ←── PRIMARY (5 arms, single process) │
│   src/live_loop_10s.py        ←── LEGACY  (self-contained loop)    │
│   trade_labs_orchestrator.py  ←── LEGACY  (pipeline orchestrator)  │
│   run_hybrid_trading.py       ←── LEGACY  (hybrid news+quant)     │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                     EVENT BUS (LocalBus / RedisBus)                 │
│                                                                     │
│   Topics:                                                           │
│     tl.ingest.market_snapshot    tl.ingest.news_event               │
│     tl.ingest.universe_candidates                                   │
│     tl.signal.trade_intent       tl.signal.watch_candidate          │
│     tl.signal.open_plan_candidate                                   │
│     tl.risk.plan_draft           tl.risk.order_blueprint            │
│     tl.risk.order_plan_approved  tl.risk.order_plan_rejected        │
│     tl.execution.order_event     tl.monitor.heartbeat               │
└───────────────────────────────┬─────────────────────────────────────┘
                                │
        ┌───────────┬───────────┼───────────┬───────────┐
        ▼           ▼           ▼           ▼           ▼
   ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
   │ INGEST  │ │ SIGNAL  │ │  RISK   │ │  EXEC   │ │ MONITOR │
   │  ARM    │ │  ARM    │ │  ARM    │ │  ARM    │ │  ARM    │
   └─────────┘ └─────────┘ └─────────┘ └─────────┘ └─────────┘
```

---

## B. Signal-to-Execution Flow Diagram

```
[IB TWS / Synthetic]
        │
        ▼
┌──────────────────┐    publishes     ┌──────────────────┐
│  INGEST ARM      │ ──────────────→  │  MarketSnapshot   │
│                  │                  │  NewsEvent         │
│  • IB snapshots  │                  └────────┬───────────┘
│  • Benzinga news │                           │
│  • GNews RSS     │                           ▼
│  • Finnhub (opt) │                  ┌──────────────────┐
│  • RSI-14 calc   │                  │  SIGNAL ARM      │
│  • RVOL calc     │                  │                  │
│  • Synthetic gen │                  │  OFF_HOURS:      │
└──────────────────┘                  │   6-component    │
                                      │   scoring →      │
                                      │   WatchCandidate │
                                      │   OpenPlanCand.  │
                                      │                  │
                                      │  PREMARKET:      │
                                      │   Expanded score │
                                      │   + RVOL + event │
                                      │   → TradeIntent  │
                                      └────────┬─────────┘
                                               │
                                               ▼
                                      ┌──────────────────┐
                                      │  RISK ARM        │
                                      │                  │
                                      │  • Position size │
                                      │  • Risk cap $50  │
                                      │  • Regime mult   │
                                      │  • Sector limits │
                                      │  • Kill switch   │
                                      │  • Allocation    │
                                      │                  │
                                      │  → PlanDraft     │
                                      │  → OrderBlueprint│
                                      │  OR rejected     │
                                      └────────┬─────────┘
                                               │
                                               ▼
                                      ┌──────────────────┐
                                      │  EXECUTION ARM   │
                                      │                  │
                                      │  • Session gate  │
                                      │  • SIM backend   │
                                      │  • Paper fill    │
                                      │  • Slippage sim  │
                                      │  • Exit intel    │
                                      │                  │
                                      │  → OrderEvent    │
                                      └──────────────────┘
```

---

## C. Monitoring / Feedback Loop Diagram

```
┌──────────────────────────────────────────────────────────────┐
│                      ALL ARMS                                │
│                                                              │
│   emit Heartbeat every ~10s                                  │
│   emit OrderEvent / PlanDraft / WatchCandidate / etc.        │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    MONITOR ARM                               │
│                                                              │
│   • Subscribes to: HEARTBEAT, NEWS_EVENT, WATCH_CANDIDATE,  │
│     TRADE_INTENT, PLAN_DRAFT, OPEN_PLAN_CANDIDATE,          │
│     ORDER_BLUEPRINT, ORDER_EVENT                             │
│   • Maintains _playbook dict (symbol → PlaybookEntry)        │
│   • Writes data/playbook_latest.json (sorted by total_score) │
│   • Displays health table (arm status, missing detection)    │
│   • Displays sector/rotation/allocation summaries            │
│   • Integrates playbook scorecard + exit intelligence        │
└──────────────────────────┬───────────────────────────────────┘
                           │
                           ▼
┌──────────────────────────────────────────────────────────────┐
│                    OUTPUT ARTIFACTS                           │
│                                                              │
│   data/playbook_latest.json    ← candidate watchlist         │
│   data/dashboard_snapshot.json ← real-time system state      │
│   logs/pipeline/*.log          ← structured logs             │
│   data/trade_history/          ← trade journal CSV           │
│   data/reports/                ← session reports             │
└──────────────────────────────────────────────────────────────┘
```

---

## Top-Level Module Map

### Root-level scripts

| File | Purpose | Status |
|------|---------|--------|
| `src/arms/dev_all_in_one.py` | **Primary entrypoint** — runs all 5 arms in single process via LocalBus | Active |
| `src/live_loop_10s.py` | Legacy self-contained 10s polling loop with IB, catalysts, bracket orders | Active (parallel system) |
| `trade_labs_orchestrator.py` | Legacy pipeline orchestrator with scheduling and reporting | Legacy |
| `run_hybrid_trading.py` | Legacy hybrid news+quant trading system | Legacy |
| `run_quant_trading.py` | Legacy quant-only scanner | Legacy |
| `run_backtest.py` | Backtesting harness | Utility |
| `src/main.py` | Scaffold stub — not used | Unused |
| `src/run_mvp.py` | Early MVP runner | Unused |

### Folder Responsibilities

| Folder | Files | Responsibility |
|--------|-------|----------------|
| `src/arms/` | 6 files, ~9700 lines | Core arms: ingest, signal, risk, execution, monitor, dev runner |
| `src/bus/` | 5 files, ~700 lines | Event bus infrastructure (LocalBus, RedisBus, topics, codec, factory) |
| `src/schemas/` | 3 files, ~400 lines | Message dataclasses (MarketSnapshot, TradeIntent, OrderBlueprint, etc.) |
| `src/risk/` | 11 files, ~2700 lines | Position sizing, risk guard, kill switch, exit intelligence, sector limits, daily PnL |
| `src/execution/` | 5 files, ~580 lines | Order placement, bracket orders, adapters, pipeline |
| `src/signals/` | 17 files, ~varied | Signal engine, candidate pool, scoring, regime, squeeze, sector intel, market mode, allocation |
| `src/analysis/` | 8 files, ~3200 lines | Observability: signal distribution, order lifecycle, trade journal, dashboard, scorecard, PnL attribution, self-tuning |
| `src/intelligence/` | 2 files, ~300 lines | Perplexity Sonar API client (isolated, not integrated) |
| `src/data/` | 11 files, ~varied | Catalyst hunter, research engine, news fetcher/scorer/sentiment, earnings calendar, sector map, IB market data |
| `src/market/` | 2 files, ~200 lines | Session detection (OFF_HOURS/PREMARKET/RTH/AFTERHOURS) with DST handling |
| `src/universe/` | 6 files, ~varied | Sector mapper, composite score, dynamic universe, scan scheduler, universe master CSV |
| `src/quant/` | 6 files, ~varied | Quantitative scanner, scorer, portfolio risk manager, hyper-swing filters, technical indicators |
| `src/monitoring/` | 2 files | Logger configuration |
| `src/broker/` | 3 files | IB connection helpers, scoring utilities |
| `src/utils/` | 8 files, ~varied | Playbook I/O, trade history DB, report generator, position reconciler, scheduler, market hours |
| `src/database/` | 3 files | SQLite DB manager, migrations, models |
| `src/patterns/` | 2 files | Playbook miner (pattern detection) |
| `src/indicators/` | 1 file | ATR calculation |
| `src/contracts/` | 2 files | Legacy TradeIntent dataclass |
| `src/config/` | 2 files | Settings loader from env |
| `config/` | 6 files | IB config, identity, risk limits, runtime mode, universe filter, example env |

---

## How Components Communicate

### Arms Architecture (Primary)

All 5 arms communicate via the **event bus** (`src/bus/`):

- **LocalBus** (`BUS_BACKEND=local`, default) — In-memory pub/sub. Single dispatcher thread pulls from a queue and routes to handlers. No Redis required.
- **RedisBus** (`BUS_BACKEND=redis`) — Redis Pub/Sub with JSON serialization via `src/schemas/codec.py`.

Messages are plain Python dataclasses defined in `src/schemas/messages.py`. Topics are string constants in `src/bus/topics.py`.

**Communication pattern:**
```
ingest  → publishes  → MARKET_SNAPSHOT, NEWS_EVENT, UNIVERSE_CANDIDATES
signal  → subscribes → MARKET_SNAPSHOT, NEWS_EVENT
signal  → publishes  → TRADE_INTENT, WATCH_CANDIDATE, OPEN_PLAN_CANDIDATE
risk    → subscribes → TRADE_INTENT, OPEN_PLAN_CANDIDATE
risk    → publishes  → PLAN_DRAFT, ORDER_BLUEPRINT, ORDER_PLAN_APPROVED/REJECTED
exec    → subscribes → ORDER_PLAN_APPROVED, ORDER_BLUEPRINT
exec    → publishes  → ORDER_EVENT
monitor → subscribes → HEARTBEAT, NEWS_EVENT, WATCH_CANDIDATE, TRADE_INTENT,
                        PLAN_DRAFT, OPEN_PLAN_CANDIDATE, ORDER_BLUEPRINT, ORDER_EVENT
all     → publish    → HEARTBEAT (periodic)
```

### Legacy Live Loop

`src/live_loop_10s.py` does not use the bus. It directly calls IB, scanner, catalyst engine, risk sizing, and bracket order functions in a single-threaded 10-second polling loop. It integrates the analysis modules (signal distribution, lifecycle, journal, dashboard) directly.

---

## Threading Model

### Arms (dev_all_in_one.py)

- **Main thread:** Signal handling, startup, shutdown coordination
- **5 daemon threads:** One per arm, each running its `main()` function
- **1 LocalBus dispatcher thread:** Routes messages from publish queue to handlers
- **Arms signal registration patched:** `signal.signal()` calls are no-op'd in daemon threads (only main thread can register signals)

### Legacy Live Loop

- **Single-threaded:** One `while True` loop with `time.sleep(10)` between iterations
- **Blocking IB calls:** Uses `ib_insync` synchronous API

---

## Important Entrypoints

| Command | What it does |
|---------|-------------|
| `python -m src.arms.dev_all_in_one` | Run all 5 arms (primary development mode) |
| `scripts/run_paper_session.sh` | Production paper session — unsets all force flags, runs dev_all_in_one |
| `scripts/premarket_check.sh` | Smoke test — runs system for 60s, checks health gates |
| `scripts/check_system.sh` | Post-hoc log analysis for health indicators |
| `python src/live_loop_10s.py` | Legacy live loop (parallel system) |
| `python -m src.arms.ingest_main` | Run ingest arm standalone |
| `python -m src.arms.signal_main` | Run signal arm standalone |
| `python -m src.arms.risk_main` | Run risk arm standalone |
| `python -m src.arms.execution_main` | Run execution arm standalone |
| `python -m src.arms.monitor_main` | Run monitor arm standalone |

---

## Runtime Flow (dev_all_in_one)

1. Set `BUS_BACKEND=local`, create shared `LocalBus`, inject via `set_shared_bus()`
2. Import all 5 arm modules: `ingest_main`, `signal_main`, `risk_main`, `execution_main`, `monitor_main`
3. Start each arm's `main()` in a daemon thread (signal registration patched to no-op)
4. Main thread registers SIGINT/SIGTERM → flips each arm's `_running` flag to False
5. Arms loop until `_running` is False, then exit
6. On shutdown: arms finalize (write artifacts, print summaries), threads join

---

## Dependencies Between Modules

```
ingest_main
  ├── src/config/settings.py (trade mode, broker config)
  ├── src/market/session.py (session detection)
  ├── src/bus/ (publish snapshots, news)
  ├── src/schemas/messages.py (MarketSnapshot, NewsEvent)
  ├── src/signals/indicators.py (compute_rsi)
  └── src/utils/playbook_io.py (load playbook symbols for premarket)

signal_main
  ├── src/config/settings.py
  ├── src/market/session.py
  ├── src/bus/ (subscribe snapshots/news, publish intents/candidates)
  ├── src/schemas/messages.py
  ├── src/signals/regime.py (trend, volatility regime)
  ├── src/signals/event_score.py (structural event gating)
  ├── src/signals/squeeze.py (Bollinger squeeze)
  ├── src/signals/sector_intel.py (sector alignment)
  ├── src/signals/volatility_leaders.py
  ├── src/signals/industry_rotation.py
  ├── src/signals/allocation_engine.py
  ├── src/signals/market_mode.py
  └── src/universe/sector_mapper.py

risk_main
  ├── src/risk/position_sizing.py (core sizing: equity × risk% / risk_per_share)
  ├── src/risk/kill_switch.py (8 circuit breakers)
  ├── src/risk/sector_limits.py (sector concentration)
  ├── src/risk/exit_intelligence.py (adaptive exit management)
  ├── src/signals/regime.py (regime risk multiplier)
  ├── src/signals/sector_intel.py
  ├── src/signals/volatility_leaders.py
  ├── src/signals/industry_rotation.py
  ├── src/signals/allocation_engine.py
  ├── src/signals/market_mode.py
  ├── src/analysis/playbook_scorecard.py
  └── src/analysis/self_tuning.py

execution_main
  ├── src/execution/orders.py (place_order with SIM/IB backends)
  ├── src/execution/adapters.py (OrderPlan → legacy OrderRequest)
  ├── src/risk/kill_switch.py (record fills/failures)
  ├── src/risk/exit_intelligence.py (register positions)
  ├── src/analysis/playbook_scorecard.py
  ├── src/analysis/pnl_attribution.py
  └── src/analysis/self_tuning.py

monitor_main
  ├── src/signals/sector_intel.py
  ├── src/signals/industry_rotation.py
  ├── src/signals/allocation_engine.py
  ├── src/signals/market_mode.py
  ├── src/universe/sector_mapper.py
  ├── src/universe/composite_score.py
  ├── src/universe/dynamic_universe.py
  ├── src/universe/scan_scheduler.py
  ├── src/risk/sector_limits.py
  ├── src/analysis/playbook_scorecard.py
  └── src/risk/exit_intelligence.py
```

---

## Current Bottlenecks and Fragility Points

1. **Two parallel systems** — `dev_all_in_one.py` and `live_loop_10s.py` are both active entrypoints with overlapping but divergent logic. Changes to one do not propagate to the other.

2. **Synthetic price contamination** — `_SYNTH_DEFAULT_SEED = 100.0` affects 26/31 symbols. All downstream calculations (entry, stop, sizing) inherit this error.

3. **No persistent state across restarts** — RSI warmup, RVOL baseline, board scores, and playbook state all reset to zero on each startup. First ~15 ticks after restart produce synthetic/unstable values.

4. **Single-threaded IB polling in ingest** — Round-robin symbol polling means stale quotes for symbols not recently polled. With 30+ symbols and 3-5s per poll, full universe refresh takes minutes.

5. **Large file sizes** — `signal_main.py` (3414 lines) and `ingest_main.py` (2466 lines) are complex monoliths with deep nesting and many env-var tunables.

6. **Feature flag sprawl** — Over 80 `TL_*` environment variables control behavior. No centralized documentation of valid combinations.

7. **Monitor arm writes JSON without rotation** — `playbook_latest.json` is overwritten atomically but has no versioning or history.
