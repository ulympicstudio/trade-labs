# UTS Operator Notes — Practical Guide

> Generated from codebase analysis. Every claim references real files, scripts, and configuration.

---

## 1. Entrypoints — How to Start UTS

### Dev All-in-One (recommended for development)

```bash
python -m src.arms.dev_all_in_one
```

Runs all 5 arms (ingest, signal, risk, execution, monitor) as daemon threads in a single process using `LocalBus` (in-memory, no Redis). Defined in `src/arms/dev_all_in_one.py`.

- Sets `BUS_BACKEND=local` automatically
- Patches `signal.signal()` to no-op in daemon threads
- Ctrl-C flips `_running` flag in all arm modules for cooperative shutdown
- Displays ASCII banner with mode (PAPER/LIVE) and cert status

### Paper Session with IB (recommended for paper trading)

```bash
./scripts/run_paper_session.sh
```

This script (`scripts/run_paper_session.sh`, 142 lines):
1. **Unsets 40+ force/test override flags** — ensures natural system behavior
2. Sets `TRADE_LABS_MODE=PAPER`, `TRADE_LABS_EXECUTION_BACKEND=IB`, `TRADE_LABS_ARMED=1`
3. Applies conservative kill-switch overrides: 2% daily loss, 10 trades/hour, 2 per symbol/hour, 3 loss streak
4. Verifies IB Gateway is reachable on port 7497 (via `nc -z`)
5. Launches `src/live_loop_10s.py` (legacy self-contained system)

**Prerequisites:** TWS or IB Gateway running in PAPER mode on port 7497.

### Legacy loop

```bash
python -m src.live_loop_10s
```

Self-contained 2,020-line loop that runs independently of the arms architecture. Uses its own catalyst engine, scanner, and bracket orders. The paper session script uses this path.

### Individual arms (requires Redis)

```bash
python -m src.arms.ingest_main
python -m src.arms.signal_main
python -m src.arms.risk_main
python -m src.arms.execution_main
python -m src.arms.monitor_main
```

Each arm connects to Redis for inter-process communication. Requires `BUS_BACKEND=redis` and a running Redis instance.

---

## 2. Pre-Session Checks

### Premarket check (60-second smoke test)

```bash
./scripts/premarket_check.sh
```

Runs the dev_all_in_one runner for 60 seconds and checks the log for:
- All 5 arm heartbeats (ingest, signal, risk, execution, monitor)
- News pipeline functional (Benzinga/GNews polling)
- At least one `MarketSnapshot` published
- Kill switch in PASS state

**Pass/fail gates:** If any arm heartbeat is missing, the check fails. Run this before market open.

### System health check (log-based)

```bash
./scripts/check_system.sh [logfile]
```

Scans a running (or completed) session log for ~50+ health indicators:
- Arm heartbeats
- News pipeline (Benzinga articles, GNews, consensus detection)
- Signal arm (RSI evaluation, TradeIntent emitted, event scores)
- Risk path (risk_path log, eventsize adjustments)
- Execution (PAPER_FILL events, order events, bus drops)
- Kill switch (BLOCK/REDUCE/PASS counts, ATR spike, loss streak)
- Sector intelligence, volatility leaders, industry rotation
- Dynamic universe, allocation engine, market mode
- Playbook scorecard, self-tuning, PnL attribution

Default log: `/tmp/tradelabs_daily.log`. Pass a custom path as argument.

### Preflight check

```bash
python preflight_check.py
```

Python-based startup validation (at the project root).

---

## 3. Configuration Reference

### Runtime mode — `config/runtime.py`

| Function | Env variable | Default | Meaning |
|----------|-------------|---------|---------|
| `mode()` | `TRADE_LABS_MODE` | `PAPER` | `PAPER` or `LIVE` |
| `is_paper()` | — | True | Derived from mode |
| `execution_backend()` | `TRADE_LABS_EXECUTION_BACKEND` | `SIM` | `SIM` (paper fill) or `IB` |
| `is_armed()` | `TRADE_LABS_ARMED` | `0` | Must be exactly `"1"` to submit orders |

**Safety:** Live mode is permanently blocked in `execution/orders.py`. Even with `TRADE_LABS_MODE=LIVE`, `place_order()` returns failure.

### IB connection — `config/ib_config.py`

| Parameter | Env variable | Default |
|-----------|-------------|---------|
| HOST | `IB_HOST` | `127.0.0.1` |
| PORT | `IB_PORT` | `7497` (paper) |
| CLIENT_ID | `IB_CLIENT_ID` | `1` |

Port 7497 = IB paper. Port 7496 = IB live (blocked by code).

### Identity — `config/identity.py`

| Parameter | Env variable | Default |
|-----------|-------------|---------|
| `SYSTEM_NAME` | `TL_SYSTEM_NAME` | `TradeLabs-UTS` |
| `OPERATOR_NAME` | `TL_OPERATOR_NAME` | `dev` |
| `MACHINE_ID` | `TL_MACHINE_ID` | hostname |

### Risk limits — `config/risk_limits.py`

| Constant | Value | Override |
|----------|-------|---------|
| `MAX_OPEN_RISK_PCT` | 0.02 (2%) | — |
| `MAX_TRADES_PER_DAY` | 10 | — |
| `DAILY_MAX_LOSS_PCT` | 0.03 (3%) | — |
| `MAX_RISK_PER_TRADE_PCT` | 0.005 (0.5%) | — |
| `MIN_UNIFIED_SCORE` | 70 | — |
| `MIN_ADV20_DOLLARS` | $25M | — |
| `PRICE_MIN` | $2 | — |
| `PRICE_MAX` | $500 | `PRICE_MAX_ALLOWLIST` for large caps |

### Key environment variables for tuning

| Variable | Default | Purpose |
|----------|---------|---------|
| `TL_ACCOUNT_EQUITY` | 100000 | Account equity for sizing |
| `TL_RISK_PER_TRADE_PCT` | 0.005 | Risk per trade (0.5%) |
| `MAX_RISK_USD_PER_TRADE` | 50 (paper) / 500 (live) | Hard dollar cap |
| `TL_TRAIL_PCT` | 1.5 | Trail stop percentage |
| `TL_ATR_MULTIPLIER` | 2.0 | ATR-to-stop multiplier |
| `TL_SIG_RSI_THRESHOLD` | 35 | RSI threshold for entry |
| `TL_SIG_SPREAD_MAX_PCT` | 0.003 | Max spread for entry |
| `TL_INGEST_INTERVAL_S` | 10 | Market data poll interval |
| `TL_INGEST_NEWS_INTERVAL_S` | 20 | News poll interval |
| `TL_INGEST_USE_IB` | 0 | Enable IB market data |
| `BASE_SYMBOLS` | SPY,QQQ,AAPL,MSFT,NVDA | Core universe |
| `TL_SYMBOLS_PER_POLL` | 40 | Symbols polled per cycle |
| `BENZINGA_API_KEY` | (required) | Benzinga news API key |

### Kill switch overrides

| Variable | Default | Purpose |
|----------|---------|---------|
| `TL_KS_DAILY_LOSS_PCT` | 0.03 (paper) / 0.02 (live) | Daily loss cap |
| `TL_KS_MAX_TRADES_HOUR` | 50 (paper) / 30 (live) | Trades per hour |
| `TL_KS_MAX_PER_SYMBOL_HOUR` | 5 (paper) / 3 (live) | Per-symbol per hour |
| `TL_KS_MAX_LOSS_STREAK` | 5 | Consecutive losers before pause |
| `TL_KILL_SWITCH` | false | Master kill switch |

---

## 4. Directory Structure for Operators

### Data files

| Path | Contents |
|------|----------|
| `data/playbook_latest.json` | Machine-readable current playbook |
| `data/playbook_latest.txt` | Human-readable summary |
| `data/diagnostic_snapshot.json` | Monitor arm dashboard state |
| `data/liquid_universe.txt` | Static liquid universe seed list |
| `data/us_symbols.json` | Valid US symbol whitelist |
| `data/trade_history/` | Session files, trade records |
| `data/reports/` | Generated reports |
| `data/research_reports/` | Perplexity research output |

### Log files

| Path | Contents |
|------|----------|
| `logs/` | Main log directory |
| `logs/pipeline/` | Pipeline-specific logs |
| `logs/paper_session_live_*.log` | Paper session logs (timestamped) |

---

## 5. Common Operating Procedures

### Starting a paper session

1. Start IB Gateway (or TWS) in paper mode → port 7497
2. Verify: `nc -z 127.0.0.1 7497`
3. Run: `./scripts/run_paper_session.sh`
4. Watch log for arm heartbeats and "Published snapshots" messages
5. Ctrl-C to stop and generate session report

### Starting a dev session (no IB needed)

1. Run: `python -m src.arms.dev_all_in_one`
2. System uses synthetic quotes (no real market data)
3. Ctrl-C to stop

### Verifying system health during a session

1. In another terminal: `./scripts/check_system.sh logs/paper_session_live_*.log`
2. Look for: all arm heartbeats present, no bus_drops, kill switch in PASS
3. Check news pipeline: Benzinga articles count > 0, GNews articles count > 0

### Stopping the system

- Ctrl-C sends SIGINT → arms flip `_running = False` → threads drain and exit
- All arms have cooperative `_stop_event` threading.Event
- `_stopping` flag in ingest arm skips all network I/O immediately

### Emergency stop

- Set env `TL_KILL_SWITCH=1` → master breaker blocks all new trades immediately
- Or Ctrl-C twice for hard exit
- Kill switch state is in-memory only; restarting clears it

---

## 6. Monitoring and Observability

### Heartbeat system

Each arm publishes `Heartbeat(arm="<name>")` messages on the bus. The monitor arm tracks last-seen timestamps and alerts if any arm goes silent.

### Key log patterns to grep

| Pattern | Meaning |
|---------|---------|
| `Trade APPROVED` | Risk arm approved a trade |
| `Trade REJECTED` | Risk arm rejected (with reason codes) |
| `PAPER_FILL` | Execution arm simulated a fill |
| `CIRCUIT_BREAKER_BLOCK` | Kill switch blocked a trade |
| `sector_limit_block` | Sector concentration blocked |
| `regime_panic_long_block` | PANIC regime blocked long entry |
| `exit_trim` | Exit intelligence trimmed a position |
| `exit_full` | Exit intelligence fully exited |
| `exit_time_stop` | Time stop triggered |
| `warmup_complete` | RSI warmup done for a symbol |
| `consensus_hits` | Multi-source news consensus detected |

### Diagnostic files

- `data/diagnostic_snapshot.json` — Monitor snapshot (updated every cycle)
- `data/playbook_latest.json` — Latest playbook (machine-readable)

---

## 7. Known Operational Issues

### 1. Synthetic prices dominate off-hours

When IB is not connected (default), all prices are synthetic random walks from seed prices. 26 of 31 symbols default to $100. Do not trust any signal, sizing, or risk metric during synthetic-price operation.

### 2. Force flags must be explicitly unset

The system has 40+ `TL_*_FORCE_*` and `TL_TEST_*` environment variables that override natural behavior. If any remain set from a dev/test session, they silently distort production behavior. Use `run_paper_session.sh` which explicitly unsets all of them.

### 3. Open risk is not dynamically tracked

The `TL_OPEN_RISK` env defaults to `0` and is not updated from actual fills. The 2% open-risk cap in `risk_guard.py` may not reflect real exposure.

### 4. Session state resets on restart

Kill switch state, RSI cache, RVOL cache, and sector/industry tracking are all in-memory. Restarting the system loses all accumulated state. Plan restarts during off-hours only.

### 5. No automated session start/stop

There is no cron job or scheduler for session lifecycle. The operator must manually start before market open and stop after close.

---

## 8. Environment File Template

See `config/settings.example.env` for a complete template. Key sections:

- Trade mode (`TRADE_LABS_MODE`)
- IB connection (`IB_HOST`, `IB_PORT`, `IB_CLIENT_ID`)
- API keys (`BENZINGA_API_KEY`, `FINNHUB_API_KEY`)
- Risk parameters
- Signal thresholds

Copy to `.env` and configure before first run:
```bash
cp config/settings.example.env .env
```

---

*Document based on codebase as of this session. No code was modified.*
