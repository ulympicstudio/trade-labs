# Upgrade Notes — Remaining Improvements (A–L)

## Summary

This upgrade adds 12 enhancements across signal, risk, execution, and ingest arms:
position sizing from event scores, consensus RSI bypass, regime strategy gating,
news category weighting, adaptive spread filter, volatility regime detection,
PAPER slippage model, session awareness, kill switch enhancements, and supporting
scripts.

All features are **env-configurable** with safe defaults that preserve existing
behaviour when left unset.

---

## New / Changed Environment Variables

### A — EventScore → Position Sizing (`risk_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_RISK_EVENTSIZE_ENABLED` | `true` | Scale qty by event score |
| `TL_RISK_EVENTSIZE_BASE` | `50` | Score that yields 1.0× sizing |
| `TL_RISK_EVENTSIZE_MIN` | `0.6` | Minimum sizing factor |
| `TL_RISK_EVENTSIZE_MAX` | `1.8` | Maximum sizing factor |

Log: `risk_eventsize symbol=XYZ event_score=72 factor=1.44 qty 10->14`

### B — Consensus RSI Bypass (`signal_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_SIG_CONSENSUS_BYPASS_RSI` | `true` | Skip RSI gate when providers ≥ threshold |
| `TL_SIG_CONSENSUS_BYPASS_MIN_PROVIDERS` | `3` | Min providers for bypass |

Log: `consensus_bypass_rsi symbol=XYZ providers=3 impact=85`

### C — Squeeze Watchlist → Universe (`ingest_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_SQUEEZE_UNIVERSE_TOP_N` | `25` | Max squeeze candidates to inject |
| `TL_SQUEEZE_MIN_SCORE` | `30` | Min squeeze score to include |

Log: `squeeze_watchlist_added n=5 sample=[SPY(82), NVDA(75)]`

### D — Regime → Strategy Gating (`signal_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_SIG_REGIME_GATE_ENABLED` | `true` | Enforce per-regime strategy allow-lists |

Gate map (in `regime.py` `STRATEGY_GATE`):
- `BULL` → all strategies
- `BEAR` → mean_revert_rsi, consensus_news
- `PANIC` → consensus_news only

Log: `regime_gate_skip regime=PANIC setup=DEV_MOMENTUM symbol=XYZ`

### E — News Category Weighting (`event_score.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_ES_CAT_EARNINGS` | `20` | Earnings category weight |
| `TL_ES_CAT_FDA` | `25` | FDA/biotech weight |
| `TL_ES_CAT_MACRO` | `15` | Macro/economic weight |
| `TL_ES_CAT_ANALYST` | `10` | Analyst upgrade/downgrade weight |
| `TL_ES_CAT_MERGER` | `20` | M&A weight |
| `TL_ES_CAT_BANKRUPTCY` | `25` | Bankruptcy/restructure weight |
| `TL_ES_CAT_CEO_CHANGE` | `12` | CEO change weight |
| `TL_ES_CAT_MGMT` | `8` | Management change weight |
| `TL_ES_CAT_GENERAL` | `5` | General news weight |
| `TL_ES_CAT_MAX_BONUS` | `25` | Cap on category bonus |

Scoring: best matching category weight, capped at `CAT_MAX_BONUS`, normalized for
the 0–1 `_W_CATEGORY` multiplier. Reason snippet: `cat=FDA(+25)`.

### F — Adaptive Spread Filter (`signal_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_SIG_SPREAD_ATR_MULT` | `0.40` | Spread limit = ATR% × this mult |
| `TL_SIG_SPREAD_MIN` | `0.0005` | Floor (0.05%) |
| `TL_SIG_SPREAD_MAX` | `0.0050` | Ceiling (0.50%) |

During OPEN phase the limit is widened by `TL_SIG_OPEN_SPREAD_WIDEN` (default `1.2`).
Falls back to static `TL_SIG_SPREAD_MAX_PCT` when ATR unavailable.

Log: `spread_gate_skip symbol=XYZ spread_pct=0.0032 limit=0.0025`

### G — Volatility Regime (`regime.py` + `risk_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_REG_VOL_HIGH_MULT` | `1.5` | ATR/baseline ratio → VOL_HIGH |
| `TL_REG_VOL_LOW_MULT` | `0.6` | ATR/baseline ratio → VOL_LOW |
| `TL_RISK_VOL_STOP_MULT` | `1.3` | Widen stop by this factor in VOL_HIGH |
| `TL_RISK_VOL_QTY_MULT` | `0.8` | Reduce qty by this factor in VOL_HIGH |

Vol regime is classified in `RegimeState.vol_regime` (LOW / NORMAL / HIGH).
When HIGH, risk arm widens stops and reduces qty.

Log: `risk_vol_regime symbol=XYZ vol=HIGH stop_mult=1.3 qty_mult=0.8 qty 10->8 stop=145.20`

### H — PAPER Slippage Model (`execution_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_EXEC_SLIPPAGE_MULT` | `0.05` | Fraction of spread applied as slippage |

Fill price computation:
- BUY: `ref_price + spread × SLIPPAGE_MULT`
- SELL: `ref_price − spread × SLIPPAGE_MULT`

Log: `PAPER_FILL symbol=SPY side=BUY ref=mid 519.53 fill=519.56 slip=0.0300`

### I — Session Awareness (`signal_main.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_SIG_SESSION_AWARE` | `true` | Enable session-phase strategy restrictions |
| `TL_SIG_MIDDAY_MIN_EVENT` | `60` | Min event score for momentum in MIDDAY |
| `TL_SIG_OPEN_SPREAD_WIDEN` | `1.2` | Spread limit widening during OPEN |

Session phases: `PREMARKET`, `OPEN`, `MIDDAY`, `POWER_HOUR`, `RTH`,
`AFTERHOURS`, `OFF_HOURS`.

Restrictions:
- **PREMARKET**: consensus_news only
- **MIDDAY**: momentum disabled unless event_score ≥ threshold

Log: `session_state=OPEN` (once per minute)

### J — Kill Switch Enhancements (`kill_switch.py`)

| Variable | Default | Description |
|---|---|---|
| `TL_KS_LOSS_STREAK_PAUSE_S` | `900` | Seconds to pause after max loss streak |
| `TL_KS_ATR_SPIKE_MULT` | `2.5` | ATR/baseline ratio to trigger spike |
| `TL_KS_ATR_SPIKE_ACTION` | `REDUCE` | `REDUCE` or `BLOCK` when spike detected |

**Loss streak**: When streak ≥ max, all orders BLOCKED for `PAUSE_S` seconds.
After pause expires, orders resume with a note.

**ATR spike**: When `atr_pct / baseline_pct ≥ ATR_SPIKE_MULT`, breaker #8
fires with configured action.

Log: `CIRCUIT_BREAKER action=BLOCK reason=loss_streak_paused ...`
Log: `CIRCUIT_BREAKER action=REDUCE reason=atr_spike_active ...`

---

## Files Modified

| File | Deliverables |
|---|---|
| `src/signals/event_score.py` | E |
| `src/signals/regime.py` | D, G |
| `src/risk/kill_switch.py` | J |
| `src/arms/signal_main.py` | A (partial), B, D, F, I |
| `src/arms/risk_main.py` | A, G |
| `src/arms/execution_main.py` | H |
| `src/arms/ingest_main.py` | C |
| `scripts/check_system.sh` | K |
| `scripts/smoke_upgrade.py` | Smoke test |
| `UPGRADE_NOTES.md` | L |

---

## Smoke Test

```bash
.venv/bin/python scripts/smoke_upgrade.py
```

Verifies all new imports, env var parsing, and function signatures without
requiring broker connectivity.

---

## Force-Path Validation & Calibration (Post-Upgrade)

### Deterministic Test-Mode Toggles

Force every conditional path to fire deterministically during E2E testing:

| Variable | Default | Effect |
|---|---|---|
| `TL_TEST_FORCE_REGIME` | `""` | Force regime to TREND_UP/TREND_DOWN/CHOP/PANIC (skip real SPY/QQQ regime) |
| `TL_TEST_FORCE_ATR_SPIKE` | `0` | Force ATR spike state (`1`=spike, `0`=normal) |
| `TL_TEST_FORCE_SQUEEZE_SCORE` | `0` | Force squeeze score (0=off, >0 returns synthetic SqueezeResult) |
| `TL_TEST_FORCE_EVENT_SCORE` | `0` | Override computed EventScore with this value |
| `TL_TEST_FORCE_CONSENSUS` | `0` | Force consensus provider count |
| `TL_TEST_FORCE_SPREAD_PCT` | `0` | Force bid-ask spread percentage (e.g. `0.002`) |

Example — force a PANIC regime with ATR spike:
```bash
TL_TEST_FORCE_REGIME=PANIC TL_TEST_FORCE_ATR_SPIKE=1 \
  python -m src.arms.dev_all_in_one
```

Valid regime values: `TREND_UP`, `TREND_DOWN`, `CHOP`, `PANIC`.

### Armed-Not-Triggered Observability

Every 60 seconds, the signal arm logs an `obs_gates` summary:
```
obs_gates event_gate=3/150 consensus_bypass=0/50 spread_gate=12/150 regime_gate=0/150
```
Format: `fired/armed` — i.e., how many checks blocked vs. how many were evaluated.

### Per-Strategy Event Gate Thresholds

Already configurable (Upgrade A):

| Variable | Default | Strategy |
|---|---|---|
| `TL_SIG_MIN_EVENT_SCORE_RSI` | `35` | mean_revert_rsi |
| `TL_SIG_MIN_EVENT_SCORE_DEV` | `25` | DEV_MOMENTUM |
| `TL_SIG_MIN_EVENT_SCORE_CONSENSUS` | `45` | consensus_news |

### Explicit Risk Path Logs

Every intent that reaches risk_main now emits a consolidated `risk_path` log:
```
risk_path symbol=AAPL conf_raw=0.450 event_score=62 regime=BULL regime_mult=1.00
  vol=NORMAL qty_raw=15 qty_final=12 risk_usd=48.00 reductions=['risk_cap']
```

### Execution Fill Explainability

PAPER_FILL logs now include computed metrics:
```
PAPER_FILL symbol=AAPL side=LONG ref=mid 185.50 fill=185.54 slip=0.0371
  slippage_bps=2.0 spread_bps=12.5 event_score=62 delay_ms=50 partial_fill=8/10
```

### Quick-Refresh Testing Mode

Already env-configurable — use for fast iteration:
```bash
UNIVERSE_REFRESH_S=30 TL_SQUEEZE_MIN_SCORE=10 \
  python -m src.arms.dev_all_in_one
```

### Premarket Go/No-Go Script

```bash
# Run system for 60s, check all gates, exit 0=GO / 1=NO-GO
./scripts/premarket_check.sh

# Check an existing log
./scripts/premarket_check.sh /tmp/tradelabs_daily.log

# Custom duration
PREMARKET_DURATION=90 ./scripts/premarket_check.sh
```

---

## Rollback

All features toggle off via env vars. To restore prior behaviour:

```bash
export TL_RISK_EVENTSIZE_ENABLED=false
export TL_SIG_CONSENSUS_BYPASS_RSI=false
export TL_SIG_REGIME_GATE_ENABLED=false
export TL_SIG_SESSION_AWARE=false
export TL_EXEC_SIM_FRICTION=false
```
