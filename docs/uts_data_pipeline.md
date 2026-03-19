# UTS Data Pipeline — Detailed Reference

> Generated from codebase analysis. Every claim references real files and functions.

---

## Overview

The data pipeline is the ingestion layer of UTS, responsible for acquiring market data and news, computing derived indicators (RSI, RVOL), and publishing enriched snapshots onto the bus for downstream arms (signal, risk, execution, monitor).

Primary file: `src/arms/ingest_main.py` (~2,466 lines).

Supporting data modules in `src/data/`:

| File | Role |
|------|------|
| `ib_market_data.py` | IB Gateway connection, historical bars, live quotes |
| `news_fetcher.py` | Benzinga API + GNews RSS + Finnhub news |
| `news_scorer.py` | News impact scoring |
| `news_sentiment.py` | Headline sentiment analysis |
| `catalyst_scorer.py` | Catalyst event scoring |
| `catalyst_hunter.py` | Catalyst discovery |
| `earnings_calendar.py` | Earnings date tracking |
| `research_engine.py` | Perplexity-based research |
| `sector_map.py` | Sector classification data |

---

## 1. Market Data Sources

### Source A: Interactive Brokers (optional)

**Enabled by:** `TL_INGEST_USE_IB=1` (default: `0` — disabled)

When enabled, `_try_connect_ib()` connects to IB Gateway/TWS using `src/data/ib_market_data.py`:
- `connect_ib()` — connects via `ib_insync` using `HOST`, `PORT`, `CLIENT_ID` from `config/ib_config.py`
- `get_last_price(ib, contract)` — requests snapshot via `reqMktData`, falls back to close, then mid, then historical bars
- `_fetch_ib_snapshot(ib, symbol)` — wraps the above into a `MarketSnapshot`

IB provides real bid/ask/last/volume for live and paper accounts.

### Source B: Synthetic quotes (default for PAPER)

When IB is not connected, `_synthetic_snapshot(symbol)` generates prices using a per-symbol random walk:

```
last = prev × (1 + N(drift, vol))
bid  = last × 0.999
ask  = last × 1.001
```

Parameters per symbol:
- **Drift:** uniform(−0.00002, +0.00002) per tick
- **Vol:** uniform(0.06%, 0.20%) per tick
- **Seed prices:** SPY=520.0, QQQ=440.0, AAPL=195.0, MSFT=420.0, NVDA=135.0
- **Default seed:** $100.0 for all other symbols

Each symbol gets a deterministic RNG seeded by `hash(symbol)`, so paths are reproducible across restarts but independent between symbols.

### Source C: Stub (zero-price fallback)

`_stub_snapshot(symbol)` returns a `MarketSnapshot` with session=`"STUB"` and all-zero prices. Used when both IB and synthetic fail.

---

## 2. News Data Sources

### Provider hierarchy

Configured by `NEWS_PROVIDERS` env (default: `benzinga,gnews`).

| Provider | Module | API key env | Default |
|----------|--------|-------------|---------|
| Benzinga | `src/data/news_fetcher.BenzingaNewsAPI` | `BENZINGA_API_KEY` | Primary |
| GNews | RSS via `feedparser` | None (public RSS) | Secondary |
| Finnhub | `TL_NEWS_ENABLE_FINNHUB` | `FINNHUB_API_KEY` | Disabled |

### News polling

- **Interval:** Every 20s (env `TL_INGEST_NEWS_INTERVAL_S`)
- **Max items per poll:** 50 from Benzinga (env `BENZINGA_NEWS_MAX_ITEMS`)
- **Max published per poll:** 100 (env `TL_NEWS_MAX_PUBLISHED_PER_POLL`)
- **Dedup window:** 3,600s by `(symbol, headline)` tuple
- **Look-back:** 1 day (env `TL_INGEST_NEWS_DAYS`)
- **Fetch timeout:** Hard timeout via `ThreadPoolExecutor` with `_NEWS_FETCH_TIMEOUT_S`

### News classification

`news_fetcher.classify_news(title)` categorizes headlines:

| Category | Keywords (examples) | Multiplier |
|----------|-------------------|------------|
| FDA | fda, approval, clinical trial | 4.0× |
| MNA | acquisition, merger, buyout | 3.5× |
| EARNINGS | earnings, revenue, eps, beat | 3.0× |
| MGMT | ceo, cfo, resign, appoint | 2.0× |
| ANALYST | upgrade, downgrade, price target | 1.5× |
| MACRO | fed, inflation, tariff, cpi | 1.0× |
| GENERAL | (default) | 1.0× |

### Ticker extraction from headlines

Ingest uses `_TICKER_RE = r"\b([A-Z]{1,5})\b"` to extract tickers from headlines, with a 200+ word blacklist (`_TICKER_BLACKLIST`) filtering common English words, media names, and acronyms. A separate `_SYMBOL_DENYLIST` blocks crypto symbols (BTC, ETH, SOL, etc.).

### Cluster-key consensus

Headlines are grouped by `_cluster_key(art)` → `"{15-min-bucket}:{symbol}:{top-3-stems}"`. When multiple sources report the same story in the same 15-minute window, a consensus boost is applied (env `TL_NEWS_CONSENSUS_BOOST=2`).

---

## 3. Derived Indicators

### RSI-14

**Computed in:** `ingest_main.py` lines ~1848–1860, using `src/signals/indicators.compute_rsi()`

- Each `MarketSnapshot.last` price is appended to `_close_cache[symbol]`
- Cache max: 200 closes per symbol
- RSI is computed from the close cache with period 14
- The enriched RSI value is written into `MarketSnapshot.rsi14` before publishing

### RSI warmup (synthetic seed)

**Function:** `_seed_rsi_warmup(symbols)` in `ingest_main.py`

To avoid needing 15 real bars before RSI produces a value, the system pre-fills each symbol's close cache with `_RSI_PERIOD + 1` (15) synthetic closes:

```
seed_price = _SYNTH_SEED_PRICES.get(sym, 100.0)
price *= (1 + N(0, 0.0003))  # ±0.03% noise per step
```

This produces an initial RSI of ~50 (neutral). Real prices replace the synthetic values as they arrive, but early RSI values are influenced by the synthetic warmup data.

### RVOL (Relative Volume)

**Function:** `_compute_rvol(symbol, current_volume)` in `ingest_main.py`

```
baseline = mean(last 20 volumes)
rvol = current_volume / baseline
```

- Lookback: 20 bars (env `TL_RVOL_LOOKBACK`)
- Returns `None` until 21 data points exist
- Volume cache max: 200 entries per symbol

---

## 4. Universe Management

### Symbol sources

The trading universe is built from multiple sources:

1. **Base symbols:** env `BASE_SYMBOLS` (default: `SPY,QQQ,AAPL,MSFT,NVDA`)
2. **Liquid universe file:** `data/liquid_universe.txt`
3. **Squeeze watchlist:** Top N symbols from squeeze scoring (env `TL_SQUEEZE_UNIVERSE_TOP_N=25`)
4. **News-discovered tickers:** Extracted from headlines during polling
5. **Playbook symbols:** Loaded from `data/playbook_latest.json`

Universe is capped at `UNIVERSE_MAX=500` symbols and refreshed every 900s (`UNIVERSE_REFRESH_S`).

### Polling strategy

Each cycle polls a subset of `TL_SYMBOLS_PER_POLL=40` symbols to spread API load. The full universe rotates across cycles.

### Symbol validation

`_validate_symbol(sym, source)` enforces:
1. 1–6 uppercase alpha characters
2. Not in `_SYMBOL_DENYLIST` (crypto)
3. Not in `_TICKER_BLACKLIST` (common words)
4. In `_valid_symbols` whitelist (loaded from `data/us_symbols.json`)

---

## 5. Data Flow Through the Pipeline

```
[IB Gateway]  ──or──  [Synthetic RNG]
        │                     │
        └─────┬───────────────┘
              │
        MarketSnapshot (raw)
              │
        ┌─────┴─────┐
        │  RSI calc  │  ← from close cache (includes synthetic warmup)
        │  RVOL calc │  ← from volume cache
        └─────┬─────┘
              │
        MarketSnapshot (enriched: rsi14, rvol)
              │
        bus.publish(MARKET_SNAPSHOT)
              │
        ┌─────┴────────────────────────────────────┐
        │                                          │
   signal_main.py                          monitor_main.py
   (trade decisions)                    (playbook, dashboard)

[Benzinga API]  ──+──  [GNews RSS]  ──+──  [Finnhub]
        │                 │                    │
        └────────┬────────┘────────────────────┘
                 │
           Dedup + classify + cluster-key consensus
                 │
           NewsEvent message
                 │
           bus.publish(NEWS_EVENT)
                 │
           signal_main.py → event_score → TradeIntent
```

---

## 6. Signal Arm Data Consumption

`src/arms/signal_main.py` subscribes to `MARKET_SNAPSHOT` and `NEWS_EVENT`, and uses the enriched data:

- **RSI-14:** Strategy trigger — RSI < 35 (env `TL_SIG_RSI_THRESHOLD`) contributes to long entry
- **Spread:** Spread < 0.3% max (env `TL_SIG_SPREAD_MAX_PCT`)
- **VWAP:** Price above VWAP as momentum filter
- **Event score:** `src/signals/event_score.compute_event_score()` produces a composite score from news + catalyst data
- **Regime:** `src/signals/regime` classifies market conditions (TREND_UP, TREND_DOWN, CHOP, PANIC) based on SPY/QQQ snapshots
- **Squeeze:** `src/signals/squeeze` detects compression patterns
- **Sector intel:** `src/signals/sector_intel` tracks sector alignment
- **Volatility leaders:** `src/signals/volatility_leaders` identifies vol expansion

### Off-hours scoring

During `OFF_HOURS` and `PREMARKET`, `signal_main.py` uses `_OffHoursScore` with 6 components:
- News score
- Momentum score
- Volume score (from cum_volume, not rvol)
- Spread score
- RSI score
- Liquidity score

**Note:** RVOL is not included in off-hours scoring. This is intentional — `_compute_rvol()` returns `None` until the volume cache has 21+ entries, which requires RTH data accumulation.

---

## 7. Playbook and Monitor Data Output

### Playbook generation

`monitor_main.py` aggregates bus messages into a live playbook:
- **`data/playbook_latest.json`** — Machine-readable snapshot of all candidates with scores, prices, stop distances, quality ratings
- **`data/playbook_latest.txt`** — Human-readable summary

Updated on every monitor cycle (default 5s interval).

### Dashboard snapshot

Stored in `data/diagnostic_snapshot.json` by the monitor arm. Contains:
- Per-arm heartbeat status
- Board state (headline counts, scores per symbol)
- Blueprint state
- Signal distribution stats

---

## Data Integrity Risks

### 1. $100 Synthetic Price Contamination (CRITICAL)

**Root cause:** `_SYNTH_DEFAULT_SEED = 100.0` in `ingest_main.py`.

Any symbol not in the 5-symbol seed price map (`SPY`, `QQQ`, `AAPL`, `MSFT`, `NVDA`) starts at $100.0 regardless of actual price. In prior audit, 26 of 31 active symbols were using synthetic $100 prices.

**Impact:** Stop distances, position sizing, notional exposure, and spread calculations are all derived from price. A $100 synthetic price for a $25 stock produces a 4× error in notional exposure and stop distance.

**Scope:** Affects all paper-mode sessions when IB is not connected (the default configuration).

### 2. RSI Warmup from Synthetic Data

**Root cause:** `_seed_rsi_warmup()` pre-fills 15 synthetic closes around the (often wrong) seed price.

RSI starts at ~50 regardless of actual market conditions. As real data arrives, the synthetic values are only slowly diluted. For the first ~15 real bars (150 seconds at 10s polling), RSI is substantially influenced by fake data.

**Impact:** Early RSI signals may trigger on artificial values. A stock might have RSI=25 in reality but RSI=48 in UTS due to synthetic warmup.

### 3. RVOL = 0.0 During Off-Hours and Early Session

**Root cause:** `_compute_rvol()` requires `_RVOL_LOOKBACK + 1` (21) volume samples before returning a value. Returns `None` until then.

During off-hours, volume from synthetic snapshots is a small random increment (500–5,000). During premarket, IB may not provide volume. The volume cache needs ~210 seconds (21 × 10s polling) to accumulate enough data.

**Impact:** RVOL is unavailable for the first ~3.5 minutes of any session, and all off-hours RVOL values are based on synthetic volume, which has no market meaning.

### 4. No Historical Price Anchoring

There is no mechanism to fetch historical closing prices at startup. Every session starts from synthetic seeds or IB snapshots (if connected). There is no day-over-day continuity of price history.

### 5. Bid/Ask Spread from Synthetic Quotes is Fixed

Synthetic quotes use `bid = last × 0.999`, `ask = last × 1.001`, producing a fixed 0.2% spread. This is unrealistic — real spreads vary by symbol, time of day, and liquidity. The fixed synthetic spread may cause the spread filter to always pass, even for illiquid symbols.

### 6. Session Label Inconsistency

Synthetic snapshots are labeled `session="SYNTH"`, IB snapshots use actual session detection, and stubs use `session="STUB"`. Downstream consumers may not consistently check the session label to adjust their behavior.

---

## Recommended Audit Priorities

1. **Connect IB for paper sessions** — Even paper accounts get real market data from IB Gateway. This eliminates synthetic price contamination and provides real spreads/volumes.

2. **Per-symbol seed prices** — If synthetic mode must be used, expand `_SYNTH_SEED_PRICES` to include all universe symbols with approximate real prices, or fetch last-known prices at startup.

3. **RSI warmup guard** — Add a `warmup_complete` flag per symbol that blocks signal generation until N real bars have been consumed. Log synthetic-bar contribution percentage.

4. **RVOL premarket guard** — The signal arm should not use RVOL values that are `None` or based on fewer than 21 real-data samples. This is currently handled (off-hours score uses volume, not rvol), but worth a explicit check.

5. **End-of-day price persistence** — Save last real close prices to disk at session end. Load them as seeds at next startup to eliminate synthetic contamination on restart.

---

*Document based on codebase as of this session. No code was modified.*
