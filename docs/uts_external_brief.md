# U.T.S. (Ulympic Trade Studio) — External System Briefing

**Date:** 2026-03-16
**Version:** Arms Architecture v1 + Legacy Live Loop
**Status:** Paper trading; no live execution enabled

---

## What U.T.S. Is

U.T.S. (Ulympic Trade Studio) is a single-operator, event-driven equity trading system built for US stock markets. It ingests market data and news, scores candidates across multiple dimensions (news catalysts, technicals, sector alignment, regime), manages risk at portfolio and position level, and routes orders through Interactive Brokers in PAPER mode.

The system is operated by a single human ("Ulympic") on a Mac Studio machine. The AI component ("Studio") assists with analysis, signal generation, and observability — but all execution authority flows through code-level safety gates, never through external AI decisions.

---

## Primary Purpose

Catalyst-driven swing trading of US equities. The system identifies stocks with active news catalysts, validates them against quantitative filters (RSI, ATR, volume, relative strength), ranks candidates by composite score, sizes positions according to risk rules, and generates bracket orders (limit entry + stop loss + trailing stop).

---

## Current Maturity

- **Paper-certified** — runs against IB paper account on port 7497
- **Live execution permanently blocked** — `place_order()` returns `ok=False` for non-PAPER mode
- **Arms architecture operational** — 5 arms communicate via in-memory LocalBus or Redis
- **Legacy live loop co-exists** — `src/live_loop_10s.py` (2020 lines) is a parallel entrypoint with its own catalyst engine, bracket orders, and risk controls
- **Observability layer recently added** — signal distribution, order lifecycle, trade journal, dashboard snapshot

---

## Main Components

| Component | Location | Purpose |
|-----------|----------|---------|
| Ingest Arm | `src/arms/ingest_main.py` (2466 lines) | Market data polling, news fetching (Benzinga, GNews, Finnhub), RSI/RVOL computation, synthetic quote generation |
| Signal Arm | `src/arms/signal_main.py` (3414 lines) | Off-hours/premarket scoring, regime detection, sector intelligence, event scoring, trade intent generation |
| Risk Arm | `src/arms/risk_main.py` (1337 lines) | Position sizing, circuit breakers, sector limits, regime risk adjustment, plan draft/blueprint generation |
| Execution Arm | `src/arms/execution_main.py` (728 lines) | Order routing (SIM or IB), paper fill simulation with slippage model, exit intelligence integration |
| Monitor Arm | `src/arms/monitor_main.py` (1355 lines) | Playbook JSON export, health table, sector/rotation display, scorecard monitoring |
| Dev Runner | `src/arms/dev_all_in_one.py` (416 lines) | Runs all 5 arms in one process using LocalBus (no Redis required) |
| Legacy Live Loop | `src/live_loop_10s.py` (2020 lines) | Self-contained 10-second polling loop with IB connection, catalyst engine, bracket orders, scanner |
| Bus System | `src/bus/` | LocalBus (in-memory) and RedisBus backends with topic-based pub/sub |
| Risk Modules | `src/risk/` | Position sizing, kill switch, sector limits, exit intelligence, daily PnL manager |
| Analysis | `src/analysis/` | Signal distribution, order lifecycle, trade journal, dashboard snapshot, PnL attribution, self-tuning |
| Intelligence | `src/intelligence/` | Perplexity Sonar API client (isolated, not yet integrated into arms pipeline) |

---

## Current Workflow

1. **Ingest** — Polls IB for market snapshots (or generates synthetic quotes in PAPER mode when IB returns zero data). Fetches news from Benzinga, GNews, and optionally Finnhub. Computes RSI-14 and RVOL. Publishes `MarketSnapshot` and `NewsEvent` messages to the bus.

2. **Signal** — Subscribes to snapshots and news. During off-hours, runs a 6-component scoring model (news, momentum, volatility, spread, RSI, liquidity) and publishes `WatchCandidate` / `OpenPlanCandidate`. During premarket, runs an expanded scoring model with RVOL, regime gating, and event scores. Publishes `TradeIntent` for actionable signals.

3. **Risk** — Receives `TradeIntent` or `OpenPlanCandidate`. Applies position sizing (`entry - stop → risk_per_share → shares`), enforces per-trade risk cap ($50 PAPER / $500 live), applies regime multiplier, volatility adjustment, sector limits, allocation engine, market mode, scorecard bias, and self-tuning nudges. Publishes `PlanDraft` or `OrderBlueprint`.

4. **Execution** — Receives `OrderPlan` or `OrderBlueprint`. Gates on session (RTH only unless `ALLOW_EXTENDED_HOURS`). Routes through adapter layer to `place_order()`. In PAPER/SIM mode, simulates fills with configurable slippage. Publishes `OrderEvent`.

5. **Monitor** — Subscribes to all message types. Maintains a playbook dictionary, writes `data/playbook_latest.json` periodically. Displays health tables, sector summaries, and scorecard data.

---

## Data Sources

| Source | Type | Status |
|--------|------|--------|
| Interactive Brokers (TWS/Gateway) | Live quotes, historical bars, account data | Active — PAPER port 7497 |
| Benzinga API | News articles | Active — primary news provider |
| GNews RSS | News articles | Active — secondary provider |
| Finnhub API | News articles, earnings calendar | Optional — enabled via `TL_NEWS_ENABLE_FINNHUB` |
| Perplexity Sonar API | AI-powered news analysis | Isolated module — not yet integrated into arms pipeline |
| Synthetic generator | Fake quotes for PAPER mode | Active — fills gaps when IB returns no data |

---

## Live vs Paper vs Experimental

- **Paper (active):** Arms pipeline + legacy live loop run against IB paper account. All order routing goes through SIM backend by default.
- **Live (permanently blocked):** `place_order()` hard-blocks non-PAPER mode. `is_armed()` requires `TRADE_LABS_ARMED=1` which is never set in production scripts.
- **Experimental:** Perplexity intelligence module exists but is not wired into the arms pipeline. Self-tuning and PnL attribution modules are integrated but lightly tested. Composite scoring (`src/universe/composite_score.py`) and dynamic universe (`src/universe/dynamic_universe.py`) are present but gated by feature flags.

---

## Current Strengths

- **Layered risk controls** — Kill switch with 8 circuit breakers, sector limits, regime gating, allocation engine, position cap
- **Event-driven architecture** — Clean pub/sub via bus system decouples arms
- **Multi-source news** — Benzinga + GNews + optional Finnhub with deduplication and consensus detection
- **Observability** — Signal distribution analysis, order lifecycle tracking, trade journal, dashboard snapshots
- **Session awareness** — Automatic detection of OFF_HOURS / PREMARKET / RTH / AFTERHOURS with appropriate behavior per session
- **Safety gates** — Live mode permanently blocked, SIM backend default, session gating on execution

---

## Current Limitations

- **Synthetic price contamination** — During off-hours, 26/31 symbols in playbook use $100 default seed price instead of real last-known prices. This makes entry prices, stop distances, and position sizing unreliable for those symbols. Only 5 symbols (SPY, QQQ, AAPL, MSFT, NVDA) have explicit seed prices.
- **RVOL absent in off-hours** — The `_OffHoursScore` dataclass has no RVOL component. All playbook entries generated off-hours show `rvol=0.0`.
- **RSI from synthetic candles** — RSI-14 is pre-seeded from 15 synthetic candles with ±0.03% noise. Values are structurally meaningless until real bars replace them.
- **Two parallel systems** — The arms architecture (`dev_all_in_one.py`) and legacy live loop (`live_loop_10s.py`) coexist with overlapping but non-identical logic. This creates maintenance burden and confusion about which entrypoint is canonical.
- **No persistent state** — Arms restart from zero on each launch. No cached prices, positions, or session state survive restarts.
- **Single-operator dependency** — No automated monitoring or alerting beyond log inspection.

---

## Known Data Integrity Concerns

1. **$100 synthetic price leak** — `_SYNTH_DEFAULT_SEED = 100.0` in `ingest_main.py:747` contaminates entry prices for all symbols not in the 5-symbol `_SYNTH_SEED_PRICES` dict.
2. **RVOL pipeline gap** — `_compute_rvol()` only runs during PREMARKET/RTH. Off-hours scoring and playbook export never populate RVOL.
3. **Stale playbook** — `playbook_latest.json` persists on disk but is not timestamped per-entry. An operator may trust off-hours data that was generated from synthetic quotes.
4. **News deduplication window** — Default 1-hour window (`NEWS_DEDUPE_WINDOW_S=3600`) may let near-duplicate articles through on slow news days.

---

## How External AI Should Assist

External AI systems (e.g., Perplexity Computer) should operate in an **advisory-only** role:

- **Catalyst analysis** — Identify and summarize news catalysts for watchlist symbols
- **Sentiment analysis** — Score headline sentiment and detect narrative shifts
- **Risk flagging** — Flag earnings dates, FDA decisions, macro events that affect open or candidate positions
- **Ranking assistance** — Provide supplementary scoring signals that the operator can manually incorporate

---

## What External AI Must NOT Do

- **Execute trades** — No external system may place, modify, or cancel orders
- **Change position sizing** — Risk parameters are set in code and environment variables only
- **Override risk engine** — Circuit breakers, sector limits, and kill switches are inviolable
- **Modify code** — External systems provide analysis, not code changes
- **Access credentials** — API keys, account numbers, and broker connection details are never shared
- **Make real-time decisions** — The human operator is the sole decision authority for any action that affects capital
