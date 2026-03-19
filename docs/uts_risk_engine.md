# UTS Risk Engine — Detailed Reference

> Generated from codebase analysis. Every claim references real files and functions.

---

## Overview

The UTS risk engine is a multi-layered system that sits between signal generation and order execution. It receives `TradeIntent` messages on the bus, applies a chain of sizing adjustments, runs circuit-breaker checks, and either publishes an `OrderPlan` (approved) or rejects the trade with reason codes.

Primary files:

| File | Lines | Role |
|------|-------|------|
| `src/arms/risk_main.py` | ~1,337 | Orchestrator — sizing chain + approval |
| `src/risk/position_sizing.py` | 49 | Core shares = max_risk / risk_per_share |
| `src/risk/risk_guard.py` | 80 | Portfolio-level gatekeeper |
| `src/risk/kill_switch.py` | ~380 | 8 circuit breakers |
| `src/risk/sector_limits.py` | ~130 | Sector / industry concentration |
| `src/risk/exit_intelligence.py` | ~600 | Post-fill exit management (7 actions) |
| `src/risk/daily_pnl_manager.py` | ~30+ | Hard kill at -1.5% equity |
| `config/risk_limits.py` | 30 | Static risk constants |

---

## 1. Position Sizing — Core Formula

**File:** `src/risk/position_sizing.py`, function `calculate_position_size()`

```
risk_per_share = entry_price - stop_price
max_risk       = account_equity × risk_percent
shares         = floor(max_risk / risk_per_share)
```

If no `stop_price` is provided, the stop is derived from ATR:

```
stop_price = entry_price - (atr × atr_multiplier)
```

Default ATR multiplier: `2.0` (env `TL_ATR_MULTIPLIER`).

---

## 2. The Sizing Adjustment Chain

Once `calculate_position_size()` returns a base quantity, `risk_main.py` applies **up to 12 sequential adjustments** before publishing the approved plan. Each adjustment can reduce (or rarely boost) `final_qty`.

### Adjustment sequence (in order)

| Step | Label | Source | Effect |
|------|-------|--------|--------|
| 1 | Risk cap | `_MAX_RISK_USD` | Clamp qty so `risk_usd ≤ cap` ($50 paper / $500 live) |
| 2 | Event size | `_EVENTSIZE_ENABLED` | Scale qty by `event_score / 50`, clamped [0.6, 1.8] |
| 3 | Regime | `_get_regime()` | Multiply by regime risk mult; PANIC blocks longs entirely |
| 4 | Vol regime | `_regime.vol_regime` | If VOL_HIGH → qty × 0.8, widen stop × 1.3 |
| 5 | Sector limit | `_check_sector_limit()` | BLOCK / REDUCE / PASS per sector concentration |
| 6 | Vol leader | `_vol_compute_leader()` | If TRIGGERED + score ≥ 65 → qty × 0.85, widen stop × 1.3 |
| 7 | Industry rotation | `_rotation_compute()` | Multiply by rotation state factor |
| 8 | Allocation engine | `_alloc_confluence()` | Bucket capacity check; BLOCK if full, else confluence mult |
| 9 | Market mode | `_mm_get_last()` | MINIMAL → × 0.60, DEFENSIVE → × 0.80, AGGRESSIVE → × 1.10 |
| 10 | Scorecard | `_sc_risk_mult()` | Historical playbook performance multiplier |
| 11 | Self-tuning | `_tune_qty_mult()` | Live win-rate nudge, clamped [0.5, 1.5] |
| 12 | Rotation bias | scan_scheduler + rotation_selector | LOW priority or rotating-out sector → × 0.70 |

After all adjustments: `final_risk_usd = risk_per_share × final_qty`.

**Key defaults** (from env or code):

| Parameter | Default | Env variable |
|-----------|---------|--------------|
| Account equity | $100,000 | `TL_ACCOUNT_EQUITY` |
| Risk per trade | 0.5% | `TL_RISK_PER_TRADE_PCT` |
| Max risk USD (paper) | $50 | `MAX_RISK_USD_PER_TRADE` |
| Max risk USD (live) | $500 | `MAX_RISK_USD_PER_TRADE` |
| Trail % | 1.5% | `TL_TRAIL_PCT` |
| Event size base | 50 | `TL_RISK_EVENTSIZE_BASE` |
| Vol qty mult | 0.80 | `TL_RISK_VOL_QTY_MULT` |
| Vol stop mult | 1.30 | `TL_RISK_VOL_STOP_MULT` |

---

## 3. Circuit Breakers (Kill Switch)

**File:** `src/risk/kill_switch.py`, function `check_circuit_breakers()`

Called after the sizing chain, before risk-guard approval. Returns `PASS`, `REDUCE`, or `BLOCK`.

### The 8 breakers

| # | Breaker | Threshold (Paper) | Threshold (Live) | Action |
|---|---------|-------------------|-------------------|--------|
| 0 | Master kill switch | env `TL_KILL_SWITCH` | same | BLOCK |
| 1 | Daily loss limit | 3% equity drawdown | 2% | BLOCK; REDUCE at 70% of limit |
| 2 | Trades per hour | 50 global, 5 per symbol | 30 / 3 | BLOCK |
| 3 | Symbol exposure | 10% equity per symbol | same | BLOCK |
| 4 | Correlated cluster | 25% equity in mega-cap cluster | same | BLOCK |
| 5 | Volatility halt | Spread > 0.5% of price | same | REDUCE × 0.50 |
| 6 | Failed orders/hour | 10 | same | BLOCK |
| 7 | Loss streak | 5 consecutive losers | same | BLOCK (300s pause) |
| 8 | ATR spike | ATR/baseline ≥ 2.5× | same | BLOCK or REDUCE × 0.50 |

**Correlated cluster symbols:** SPY, QQQ, AAPL, MSFT, GOOG, GOOGL, AMZN, NVDA, META, TSLA.

**State management:** Module-level globals (`_trade_timestamps`, `_symbol_exposure`, `_session_pnl`, etc.) are reset by `reset_session()` at session open. Functions `record_trade()`, `update_pnl()`, `record_failed_order()`, `record_fill()` track state across the session.

**Daily loss nuance:** Breaker 1 measures drawdown from session high-water mark, not from start. If PnL was +$500 then fell to -$200, the drawdown is $700, not $200.

---

## 4. Portfolio-Level Gatekeeper (Risk Guard)

**File:** `src/risk/risk_guard.py`, function `approve_new_trade()`

Called after circuit breakers pass. This is the final gate before an `OrderPlan` is published.

### Checks (in order)

1. **Trading halted** — If `state.trading_halted` is True, reject with reason.
2. **Max trades per day** — `trades_taken_today ≥ 10` → reject.
3. **Open risk cap** — `open_risk_usd + proposed_risk > equity × 2%` → reject.
4. **Per-trade risk cap** — `proposed_risk > equity × 0.5%` → reject.

### Static limits from `config/risk_limits.py`

| Constant | Value | Meaning |
|----------|-------|---------|
| `MAX_OPEN_RISK_PCT` | 0.02 | 2% of equity at risk across all open positions |
| `MAX_TRADES_PER_DAY` | 10 | Hard cap on daily trades |
| `DAILY_MAX_LOSS_PCT` | 0.03 | 3% daily loss halts new entries |
| `MAX_RISK_PER_TRADE_PCT` | 0.005 | 0.5% equity per trade |

### Known issue

`_DEFAULT_OPEN_RISK_USD` is initialized from `TL_OPEN_RISK` env (default `0`). This value is **not updated dynamically** from actual open positions. In `risk_main.py`, it remains a static initial value, meaning the open-risk cap check in `risk_guard.approve_new_trade()` may not accurately reflect real portfolio exposure unless the env variable is externally maintained.

---

## 5. Sector and Industry Concentration

**File:** `src/risk/sector_limits.py`

### Sector limits

- **Max active positions per sector:** 3
- **Max drafts (off-hours plans) per sector:** 5
- **WEAK sector state:** qty multiplier × 0.70
- **Verdicts:** `PASS`, `REDUCE`, `BLOCK`

Sector classification comes from `src/universe/sector_mapper.py`, using `classify_symbol()`.

### Industry limits

Separate concentration tracking via `check_industry_limit()`, `record_industry_fill()`, `record_industry_close()`.

### Integration with risk_main.py

At step 5 of the sizing chain, `_check_sector_limit()` is called with the symbol, its sector state (from `sector_intel.get_sector_alignment()`), and proposed notional. A `BLOCK` verdict immediately rejects the trade. A `REDUCE` verdict applies `qty_mult` to size down.

---

## 6. Daily PnL Manager

**File:** `src/risk/daily_pnl_manager.py`

A separate hard kill switch outside the bus-based system.

- **Kill threshold:** −1.5% of session-start equity (`DAILY_LOSS_THRESHOLD = -0.015`)
- Tracks realized PnL, unrealized PnL, and total daily PnL
- Stores session state in `data/trade_history/session.json`
- Uses direct IB connection for account data

This is more conservative than the 3% daily-loss value in `config/risk_limits.py`, providing a second-line hard stop.

---

## 7. Regime Gating

**Source:** `src/signals/regime.py` (imported in `risk_main.py`)

The regime module classifies current market conditions. In the risk arm:

- **PANIC regime + LONG direction:** Trade is immediately rejected (`regime_panic_long_block`).
- **Regime multipliers:** Each regime has an associated risk multiplier applied to quantity (step 3 in the chain).
- **VOL_HIGH vol regime:** Triggers step 4 — qty × 0.80 and stop widened × 1.30.
- **ATR spike:** `update_atr_spike()` feeds the kill switch; if `atr_pct / baseline_pct ≥ 2.5`, breaker 8 activates.

---

## 8. Exit Intelligence — Post-Entry Risk Management

**File:** `src/risk/exit_intelligence.py`

This module manages open positions after fill, determining when to adjust stops, trail, trim, or exit.

### Position tracking

Each fill is registered via `register_fill()`, creating a `PositionState` with:
- Entry price, stop, target (default 2R above entry)
- Symbol metadata: playbook, sector, industry, regime, market mode, volatility state
- Running stats: unrealized PnL, R-multiple, MFE/MAE, bars in trade, elapsed time, trims done

### 7 exit actions

| Action | Description |
|--------|-------------|
| `HOLD` | No action needed |
| `TIGHTEN_STOP` | Move stop closer (e.g., breakeven lock) |
| `WIDEN_STOP` | Move stop wider (vol-triggered early bars) |
| `TRAIL` | Enable trailing stop at specified % |
| `TRIM_25` | Sell 25% of position |
| `TRIM_50` | Sell 50% of position |
| `EXIT_FULL` | Close entire position |

### Decision layers (priority order in `compute_exit_decision()`)

1. **Force-path override** — Diagnostic `_FORCE_ACTION` env variable
2. **Time-stop** — Exit if elapsed ≥ limit AND unrealized PnL ≤ 0
3. **Playbook-specific rules** — Separate rule sets per playbook type
4. **Market-mode adjustments** — Tighten in defensive modes
5. **Scorecard feedback** — Reduce confidence in poorly-performing playbooks
6. **R-multiple trim thresholds** — Trim 25% at ≥ 2.0R, trim 50% at ≥ 3.5R

### Time stops by playbook

| Playbook | Time limit |
|----------|-----------|
| Mean-revert | 30 min |
| Default / unknown | 2 hr |
| Chop | 45 min |
| Defensive | 20 min |

Time stops only trigger if the position is at or below breakeven (PnL ≤ 0).

### Playbook-specific rule sets

- **News / Breakout (`_rules_news_breakout`):** Let runners run at ≥ 1.5R if trend-expanding + strong scorecard; tighten to breakeven at 1R; hard tighten at ≤ −0.75R.
- **Rotation (`_rules_rotation`):** Trail loosely if LEADING/ROTATING_IN at ≥ 1R; trim 25% if ROTATING_OUT at ≥ 1.5R; tighten if weakening below that.
- **Volatility (`_rules_volatility`):** Wide stop for first 3 bars if TRIGGERED; aggressive trail at ≥ 2R; breakeven lock at ≥ 1R.
- **Mean-revert (`_rules_meanrevert`):** Trim 50% at ≥ 1R (quick profit); breakeven lock at ≥ 0.5R; full exit if ≤ −0.5R (failed reversion).
- **Generic (`_rules_generic`):** Trail at ≥ 1.5R; tighten at ≥ 1R; standard thresholds.

### Trail percentages

| Constant | Value |
|----------|-------|
| `_TRAIL_TIGHT_PCT` | (tight, for mean-revert / fading) |
| `_TRAIL_DEFAULT_PCT` | (standard, from env `TL_TRAIL_PCT` = 1.5%) |
| `_TRAIL_LOOSE_PCT` | (wide, for runners in trend expansion) |

---

## 9. Escalation Mode (News Shock Engine v1)

**File:** `src/arms/risk_main.py` (top-level config)

Disabled by default (`TL_ESCALATION_ENABLED=false`). When enabled:

- **Impact minimum:** Event score ≥ 6
- **Vol rising minimum:** 5% rise in vol
- **Spread maximum:** 0.25%
- **Effects:** Size × 1.5, ladder widen +5 bps, trail tighten × 0.85

This allows the system to increase position size during high-conviction news events with confirming volatility.

---

## 10. Heat Cap (Legend Phase 1)

**File:** `src/arms/risk_main.py` (top-level config)

Disabled by default (`TL_HEAT_CAP_ENABLED=false`). When enabled:

- **Max open positions:** 5 (`TL_HEAT_MAX_OPEN_POS`)
- **Max total risk:** 2% equity (`TL_HEAT_MAX_TOTAL_RISK_PCT`)

Provides a hard-cap overlay on total portfolio heat independent of other checks.

---

## 11. Complete Risk Evaluation Flow

When a `TradeIntent` arrives on the bus:

```
TradeIntent received
  │
  ├─ [1] Calculate base position size
  │       shares = (equity × 0.5%) / risk_per_share
  │
  ├─ [2] Apply 12-step sizing chain
  │       risk_cap → event_size → regime → vol_regime
  │       → sector → vol_leader → rotation → allocation
  │       → market_mode → scorecard → self_tuning → rotation_bias
  │       (any step can reduce qty; two can boost)
  │
  ├─ [3] Circuit breaker check (8 breakers)
  │       BLOCK → reject immediately
  │       REDUCE → apply size_mult
  │
  ├─ [4] Risk guard approval
  │       halted? / max trades? / open risk cap? / per-trade cap?
  │       REJECT → publish ORDER_PLAN_REJECTED
  │
  └─ [5] Publish ORDER_PLAN_APPROVED
          includes bracket params, trail config, exit intelligence metadata
```

Post-fill, `exit_intelligence` tracks each position and publishes exit decisions on every heartbeat update.

---

## Current Risk Controls — Summary

### Implemented and active

- Position sizing from equity + stop distance
- $50 paper / $500 live per-trade risk cap
- 12-step quantity adjustment chain (all steps implemented)
- 8 circuit breakers in kill_switch.py
- Portfolio-level gatekeeper (risk_guard.py)
- Sector concentration limits (3 active, 5 draft)
- Daily PnL hard kill at −1.5% equity
- PANIC regime blocks long entries
- Exit intelligence with per-playbook rules, time stops, R-multiple trims
- Correlated mega-cap cluster exposure limit (25%)

### Implemented but disabled by default

- Escalation mode (news shock sizing boost)
- Heat cap (max positions + total risk overlay)

---

## Missing or Incomplete Risk Controls

### 1. Open risk not tracked dynamically
`_DEFAULT_OPEN_RISK_USD` in `risk_main.py` defaults to `0` from env and is never updated from actual fills. The `approve_new_trade()` check thus always sees near-zero open risk unless manually set. **This means the 2% open-risk cap in `risk_guard.py` is effectively bypassed.**

### 2. No short-side risk management
All sizing and stop calculations assume LONG direction (`risk_per_share = entry - stop`). Short entries would produce negative risk per share. The system blocks live mode entirely and primarily generates long signals, but no explicit short-side handling exists.

### 3. No overnight/gap risk
No pre-market gap risk adjustment. Positions held through an overnight gap have no protection beyond the static stop price, which may gap through.

### 4. Kill switch state is module-level globals
`kill_switch.py` uses module-level mutable state (`_trade_timestamps`, `_session_pnl`, etc.). In multi-process or restarted scenarios, this state is lost. `reset_session()` must be called at session open.

### 5. No max-drawdown-from-peak across days
`daily_pnl_manager.py` tracks intraday PnL only. There is no multi-day drawdown tracking or equity curve monitoring.

### 6. Trades-taken-today defaults to 0
`RiskState.trades_taken_today` defaults to `0` and is passed in from `risk_main.py` as a fresh object each call. The trades-per-day check in `risk_guard.py` may not accurately count trades unless the state is persisted across calls within the same session.

### 7. No correlation-aware risk beyond the mega-cap cluster
The correlated cluster is a hardcoded list of 10 symbols. There is no dynamic correlation measurement or sector-pair correlation tracking.

### 8. Exit intelligence has no broker-side enforcement
Exit decisions (trims, tighten stop) are computed locally but must be executed through the execution arm's paper-fill simulator. If the execution arm fails or disconnects, exit decisions are not enforced. There is no broker-side trailing stop or OCO bracket managed independently.

---

*Document based on codebase as of this session. No code was modified.*
