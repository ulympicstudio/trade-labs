# U.T.S. Execution Logic

**Date:** 2026-03-16
**Primary files:** `src/arms/execution_main.py`, `src/execution/orders.py`, `src/execution/bracket_orders.py`, `src/execution/adapters.py`

---

## How Trade Intents Are Formed

### Arms Pipeline (Primary)

1. **Ingest arm** publishes `MarketSnapshot` and `NewsEvent` to the bus.
2. **Signal arm** scores symbols and publishes:
   - `WatchCandidate` — off-hours watchlist item (informational)
   - `OpenPlanCandidate` — actionable off-hours candidate with `suggested_entry`, `suggested_stop`
   - `TradeIntent` — fully scored signal with `entry_zone_low`, `entry_zone_high`, `invalidation` price
3. **Risk arm** receives intents/candidates, sizes them, and publishes:
   - `PlanDraft` — risk-reviewed plan with qty, risk_usd, stop (not yet an order)
   - `OrderBlueprint` — fully parameterized bracket order ready for submission
   - `OrderPlan` — concrete order instructions (entry type, limit prices, stop, trail params)

### Legacy Live Loop (Parallel)

`src/live_loop_10s.py` forms trade candidates through a different path:
1. **IB scanner** (`scan_us_most_active_stocks`) + **Catalyst engine** (`ResearchEngine`) find candidates
2. **Hyper-swing filters** (`passes_hyper_swing_filters`) gate on ADV, ATR%, volume accel, RS vs SPY
3. **Score candidates** (`score_scan_results`) produce unified scores
4. Directly calls `place_limit_tp_trail_bracket()` for bracket order submission

---

## Message Types Representing Candidates and Trade Intents

All defined in `src/schemas/messages.py`:

| Class | Purpose | Key Fields |
|-------|---------|------------|
| `MarketSnapshot` | Point-in-time quote + indicators | `symbol`, `last`, `bid`, `ask`, `volume`, `rsi14`, `rvol`, `atr`, `vwap` |
| `TradeIntent` | Scored trade idea for risk review | `symbol`, `direction`, `confidence`, `entry_zone_low`, `entry_zone_high`, `invalidation`, `reason_codes` |
| `WatchCandidate` | Off-hours watchlist item | `symbol`, `score`, `news_points`, `momentum_pts`, `vol_points`, `spread_points`, `rsi_points`, `liq_points`, `quality` |
| `OpenPlanCandidate` | Off-hours actionable candidate | All WatchCandidate fields + `suggested_entry`, `suggested_stop`, `event_score`, `tradeable`, `regime`, `sector`, `composite_score` |
| `PlanDraft` | Risk-reviewed plan (no order) | `symbol`, `suggested_entry`, `suggested_stop`, `qty`, `risk_usd`, `confidence`, `stop_distance_pct`, score breakdown |
| `OrderBlueprint` | Bracket order ready to submit | `symbol`, `qty`, `entry_ladder` (3-5 prices), `stop_price`, `trail_pct`, `take_profit_levels`, `timeout_s`, `max_spread_pct`, `risk_usd` |
| `OrderPlan` | Concrete order instructions | `symbol`, `qty`, `entry_type`, `limit_prices`, `stop_price`, `trail_params`, `tif`, `timeout_s` |
| `OrderEvent` | Lifecycle event | `symbol`, `event_type`, `order_id`, `status`, `filled_qty`, `avg_fill_price`, `message` |

---

## How Orders Are Constructed

### Arms Pipeline Path

1. **Risk arm** (`risk_main.py`) receives `TradeIntent`:
   - Computes `entry_price = (entry_zone_low + entry_zone_high) / 2`
   - Computes `stop_price = intent.invalidation`
   - Computes `risk_per_share = abs(entry_price - stop_price)`
   - Calls `calculate_position_size()` → shares = `(equity × risk_pct) / risk_per_share`
   - Applies up to 10 sequential adjustments (risk cap, event sizing, regime, volatility, sector, rotation, allocation, market mode, scorecard, self-tuning)
   - Publishes `PlanDraft` for the monitor / playbook

2. **Risk arm** also generates `OrderBlueprint` during PREMARKET:
   - Entry ladder: 5 price levels spread ±0.05% around entry
   - Stop price: volatility-adjusted
   - Trail pct: 0.3%–1.0% (environment-tunable)
   - Timeout: 120 seconds
   - Max spread: 0.25% bid-ask

3. **Execution arm** (`execution_main.py`) receives `OrderPlan` (from approved blueprint):
   - Calls `plan_to_order_request()` adapter → `OrderRequest(symbol, side, quantity, order_type, stop_loss)`
   - Calls `place_order(req, ib=_ib)` → routes to SIM or IB backend

### Legacy Live Loop Path

1. Directly constructs `BracketParams(symbol, qty, entry_limit, stop_loss, trail_amount)`
2. Calls `place_limit_tp_trail_bracket(ib, params)` → IB bracket order:
   - Parent: BUY LMT at entry
   - Child A: SELL STP at stop_loss (hard downside)
   - Child B: SELL TRAIL (upside capture, if trail_amount > 0)

---

## Current Order Types Supported

| Order Type | Where Used | Status |
|------------|-----------|--------|
| Market (MKT) | `execution/orders.py` — SIM backend default | Active |
| Limit (LMT) | `execution/bracket_orders.py` — entry leg | Active |
| Stop (STP) | `execution/bracket_orders.py` — stop-loss child | Active |
| Trailing Stop | `execution/bracket_orders.py` — upside capture child | Active (IB native trail) |
| Stop-Limit (STP_LMT) | Mapped in `execution/adapters.py` | Mapped but unused in practice |

---

## Paper Execution Behavior

### SIM Backend (`execution_backend() == "SIM"`)

In `src/execution/orders.py`, the SIM backend:
```
if backend == "SIM":
    return OrderResult(ok=True, mode="PAPER", backend="SIM", ...)
    # No broker interaction — instant "success"
```

### Paper Fill Simulation (`execution_main.py`)

When `EXECUTION_ENABLED=false` (default) and `SIM_FRICTION=true`:
- Reference price = first entry ladder price or stop price
- Slippage: `+2 bps` (configurable via `TL_EXEC_SIM_SLIPPAGE_BPS`)
- Fill price: `ref × (1 + slippage)`
- Publishes `OrderEvent(event_type="PAPER_FILL")`
- Registers position with exit intelligence for adaptive exit tracking
- Records with PnL attribution and playbook scorecard

### Sim Friction Settings

| Env Var | Default | Purpose |
|---------|---------|---------|
| `TL_EXEC_SIM_FRICTION` | `true` (PAPER) | Enable simulated fills with friction |
| `TL_EXEC_SIM_DELAY_MS` | `250` | Simulated execution delay (not yet used in fill path) |
| `TL_EXEC_SIM_SLIPPAGE_BPS` | `2` | Slippage in basis points |
| `TL_EXEC_SIM_PARTIAL_FILL_PCT` | `0.6` | Partial fill percentage (defined but not active) |

---

## Live Execution Hooks

### IB Backend (`execution_backend() == "IB"`)

In `src/execution/orders.py`:
- Requires `is_armed() == True` (`TRADE_LABS_ARMED=1`)
- Requires `is_paper() == True` (LIVE mode is **permanently blocked** with a hard return)
- Creates `Stock(symbol, "SMART", "USD")` contract
- Places `MarketOrder("BUY", qty)` via `ib.placeOrder()`
- Optionally places `StopOrder("SELL", qty, stop_loss)` if stop_loss is set

### Bracket Orders (`src/execution/bracket_orders.py`)

`place_limit_tp_trail_bracket(ib, params)`:
- Qualifies contract via `ib.qualifyContracts()`
- Parent: `LimitOrder("BUY", qty, entry_limit)` with `transmit=False`
- Child A: `StopOrder("SELL", qty, stop_loss)` linked to parent via `parentId`
- Child B (optional): Trailing stop order linked via OCA group
- Returns `BracketResult` with parent_id, stop_id, trail_id, degraded flag

`place_trailing_stop(ib, symbol, qty, trail_amount)`:
- Standalone trailing stop (used after entry fill for deferred trail activation)

---

## Order Lifecycle Tracking

### Analysis Module (`src/analysis/order_lifecycle.py`)

`LifecycleLogger` tracks 15 event types through a state machine:
- `CANDIDATE_SCORED` → `FILTER_PASS` / `FILTER_REJECT`
- `FILTER_PASS` → `RISK_APPROVED` / `RISK_REJECTED`
- `RISK_APPROVED` → `ORDER_WORKING` → `PARTIAL_FILL` / `FILL` / `CANCELLED` / `REJECTED`
- `FILL` → `POSITION_OPEN` → `STOP_TRIGGERED` / `TRAIL_TRIGGERED` / `MANUAL_EXIT` → `POSITION_CLOSED`

Valid transitions are enforced by `VALID_TRANSITIONS` dict. Invalid transitions are logged but not blocked.

### Bus-Level Events

`OrderEvent.event_type` values: `"NEW"`, `"PARTIAL"`, `"FILLED"`, `"CANCELLED"`, `"REJECTED"`, `"SUBMITTED"`, `"PAPER_FILL"`

---

## Known Issues

1. **Trailing stop order placement is fragile** — In `bracket_orders.py`, trailing stop child attachment via OCA group requires IB to accept all three legs atomically. If the trail child fails, `BracketResult.degraded = True` is returned but the entry+stop legs are already placed.

2. **PAPER_FILL creates synthetic PnL** — In `execution_main.py`, the exit intelligence update loop simulates price drift with `entry_price * (1.0 + 0.005 * (tick % 10))` which creates artificial PnL movement for scorecard/attribution.

3. **Partial fills not fully handled** — `_SIM_PARTIAL_FILL_PCT = 0.6` is defined but the paper fill path always fills the full `plan.qty`. Partial fill logic is not implemented.

4. **Session gating is strict** — Execution arm only processes orders during RTH unless `ALLOW_EXTENDED_HOURS=true` or `TL_TEST_FORCE_SESSION` is set. Off-hours plan drafts go to the playbook but never reach execution.

5. **Duplicate event potential** — The bus can deliver the same `ORDER_PLAN_APPROVED` to the execution arm if the bus reconnects. No idempotency guard exists on the execution side.

---

## Execution Safeguards

| Safeguard | Location | Description |
|-----------|----------|-------------|
| Live mode hard block | `orders.py:place_order()` | Returns `ok=False` if `not is_paper()` — live mode permanently disabled |
| Armed gate | `orders.py:place_order()` | IB backend requires `TRADE_LABS_ARMED=1` |
| Session gate | `execution_main.py:_on_order_plan()` | Rejects during non-RTH unless explicitly overridden |
| SIM default | `config/runtime.py` | `TRADE_LABS_EXECUTION_BACKEND` defaults to `"SIM"` |
| Kill switch integration | `execution_main.py` | Records fills and failures with `src/risk/kill_switch.py` |
| Max spread check | `OrderBlueprint.max_spread_pct` | 0.25% default — checked before entry (blueprint level) |
| Timeout | `OrderBlueprint.timeout_s` | 120s default — cancel if unfilled |

---

## Key Files and Functions

| File | Key Functions/Classes |
|------|----------------------|
| `src/arms/execution_main.py` | `main()`, `_on_order_plan()`, `_on_order_blueprint()`, `connect_broker()` |
| `src/execution/orders.py` | `place_order()`, `OrderRequest`, `OrderResult`, `FINGERPRINT` |
| `src/execution/bracket_orders.py` | `place_limit_tp_trail_bracket()`, `place_trailing_stop()`, `BracketParams`, `BracketResult` |
| `src/execution/adapters.py` | `plan_to_order_request()`, `result_to_order_event()` |
| `src/execution/pipeline.py` | `execute_trade_intent_paper()` — legacy end-to-end pipeline |
| `src/risk/exit_intelligence.py` | `register_fill()`, `update_position_state()`, `compute_exit_decision()`, `ExitDecision` |
| `src/analysis/order_lifecycle.py` | `LifecycleLogger`, `OrderEvent` (analysis), `VALID_TRANSITIONS` |

---

## Step-by-Step Execution Flow (Arms Pipeline)

```
1. Signal arm publishes TradeIntent to bus topic tl.signal.trade_intent
       ↓
2. Risk arm _on_trade_intent() handler fires
       ↓
3. Compute entry_price = (zone_low + zone_high) / 2
   Compute stop_price = intent.invalidation
   Compute risk_per_share = |entry - stop|
       ↓
4. calculate_position_size(equity, risk_pct, entry, stop) → shares
       ↓
5. Apply adjustments: risk_cap → event_size → regime → volatility →
   sector_limits → vol_leader → rotation → allocation → market_mode →
   scorecard → self_tuning → rotation_bias
       ↓
6. check_circuit_breakers() → PASS / BLOCK
       ↓
7a. If rejected: publish to ORDER_PLAN_REJECTED with reason codes
7b. If approved: publish OrderPlan to ORDER_PLAN_APPROVED
                  publish PlanDraft to PLAN_DRAFT (for monitor/playbook)
       ↓
8. Execution arm _on_order_plan() handler fires
       ↓
9. Session gate: reject if not RTH (unless override)
       ↓
10. plan_to_order_request() adapts OrderPlan → legacy OrderRequest
       ↓
11. place_order(req, ib) routes to SIM or IB backend
       ↓
12. SIM: instant ok=True, no broker interaction
    IB: qualifyContracts → placeOrder → wait for fill
       ↓
13. Paper fill simulation (if SIM_FRICTION=true):
    Fill at ref_price + slippage, publish PAPER_FILL OrderEvent
       ↓
14. Register with exit_intelligence, pnl_attribution, scorecard
       ↓
15. Monitor arm records OrderEvent in board/playbook
```

---

## Current Execution Gaps

- **No real partial fill handling** — partial fill percentage is configured but not implemented
- **No order amendment/modification** — once submitted, orders cannot be modified through the execution arm
- **No fill confirmation loop** — PAPER_FILL is instant; no async fill monitoring for IB orders
- **No position reconciliation in arms** — the legacy `position_reconciler.py` exists but is not wired into the arms pipeline
- **Trailing stop activation is deferred** — bracket orders can start without the trail child; `place_trailing_stop()` is called separately but the trigger logic for activation is in the live loop, not the arms pipeline
- **Dev harness synthetic fills** — `_DEV_HARNESS_ENABLED` generates fake fills on a timer for testing, using hardcoded symbols (AAPL, MSFT, NVDA)

---

## What External AI Should Never Control

1. **Order submission** — No external system may call `place_order()`, `place_limit_tp_trail_bracket()`, or publish to `ORDER_PLAN_APPROVED`
2. **Position modification** — No external system may call `place_trailing_stop()`, cancel orders, or trigger exits
3. **Risk parameter overrides** — `TRADE_LABS_ARMED`, `EXECUTION_ENABLED`, `MAX_RISK_USD_PER_TRADE`, kill switch thresholds
4. **Session gate bypass** — `TL_TEST_FORCE_SESSION`, `FORCE_SESSION`, `ALLOW_EXTENDED_HOURS`
5. **Broker connection** — IB host/port/client_id configuration
6. **Account operations** — Equity queries, position queries, order history
