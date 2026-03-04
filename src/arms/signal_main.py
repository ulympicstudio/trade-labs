"""Signal Arm — signal generation & scoring.

Subscribes to ``MARKET_SNAPSHOT`` and ``NEWS_EVENT`` on the Redis bus,
maintains a rolling per-symbol cache, and runs a simple mean-reversion
strategy:

    RSI-14 < threshold  AND  price > VWAP  AND  spread < max spread
    → emit a LONG :class:`TradeIntent`

All thresholds are parameterised via ``TL_SIG_*`` env vars.

Run::

    python -m src.arms.signal_main
"""

from __future__ import annotations

import json
import math
import os
import signal
import statistics
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Set

from src.config.settings import settings
from src.monitoring.logger import get_logger
from src.bus.topics import (
    MARKET_SNAPSHOT, NEWS_EVENT, TRADE_INTENT, HEARTBEAT,
    WATCH_CANDIDATE, OPEN_PLAN_CANDIDATE,
)
from src.schemas.messages import (
    MarketSnapshot,
    NewsEvent,
    TradeIntent,
    Heartbeat,
    WatchCandidate,
    OpenPlanCandidate,
)
from src.market.session import get_us_equity_session, OFF_HOURS, PREMARKET
from src.utils.playbook_io import load_playbook_symbols, load_playbook_drafts

log = get_logger("signal")

# ── Tunables (all overridable via env) ───────────────────────────────
_RSI_THRESHOLD = float(os.environ.get("TL_SIG_RSI_THRESHOLD", "35"))
_SPREAD_MAX_PCT = float(os.environ.get("TL_SIG_SPREAD_MAX_PCT", "0.003"))  # 0.3 %
_CONFIDENCE_BASE = float(os.environ.get("TL_SIG_CONFIDENCE_BASE", "0.70"))
_CONFIDENCE_NEWS_BOOST = float(os.environ.get("TL_SIG_NEWS_BOOST", "0.10"))
_ATR_STOP_MULT = float(os.environ.get("TL_SIG_ATR_STOP_MULT", "2.0"))
_COOLDOWN_S = float(os.environ.get("TL_SIG_COOLDOWN_S", "60"))  # min seconds between intents per symbol
_CACHE_MAX_SNAPS = int(os.environ.get("TL_SIG_CACHE_MAX_SNAPS", "50"))
_CACHE_MAX_NEWS = int(os.environ.get("TL_SIG_CACHE_MAX_NEWS", "20"))
_NEWS_RECENCY_S = float(os.environ.get("TL_SIG_NEWS_RECENCY_S", "300"))  # 5 min
_FORCE_INTENT = os.environ.get("SIGNAL_FORCE_INTENT", "false").lower() in ("1", "true", "yes")
_FORCE_INTENT_INTERVAL_S = 60.0  # emit a forced intent every N seconds

# Dev-strategy flag: auto-enabled in PAPER + local bus, or explicit override
_DEV_STRATEGY_DEFAULT = (
    settings.is_paper
    and os.environ.get("BUS_BACKEND", "local").lower() == "local"
)
_DEV_STRATEGY = os.environ.get(
    "SIGNAL_DEV_STRATEGY", str(_DEV_STRATEGY_DEFAULT)
).lower() in ("1", "true", "yes")

_MAX_INTENTS_PER_SYMBOL_PER_HOUR = 3

# ── OFF_HOURS board settings ─────────────────────────────────────────
_OFF_HOURS_PUBLISH_INTERVAL_S = 30.0
_OFF_HOURS_TOP_N = 20
_OFF_HOURS_NEWS_WINDOW_S = 7200.0  # 2 h

# ── OFF_HOURS v2 scoring weights ─────────────────────────────────────
_W_NEWS = 3.0
_W_LIQ = 2.0
_W_VOL = 2.0
_W_MOM = 1.0
_W_SPREAD = 1.0
_W_RSI = 1.0
# MAX_TOTAL = 3*3 + 3*2 + 3*2 + 3*1 + 1*1 + 2*1 = 9+6+6+3+1+2 = 27
_OFF_HOURS_MAX_WEIGHTED = (
    3.0 * _W_NEWS + 3.0 * _W_LIQ + 3.0 * _W_VOL
    + 3.0 * _W_MOM + 1.0 * _W_SPREAD + 2.0 * _W_RSI
)  # 27.0
_OFF_HOURS_MAX_PRICE = float(os.environ.get("TL_SIG_MAX_PRICE", "5000"))

# ── OFF_HOURS v2 diversity controls ──────────────────────────────────
_PLAYBOOK_MAX_PER_PREFIX = int(os.environ.get("PLAYBOOK_MAX_PER_PREFIX", "3"))
_PLAYBOOK_MAX_BASE = int(os.environ.get("PLAYBOOK_MAX_BASE", "5"))
_PLAYBOOK_MAX_NON_NEWS = int(os.environ.get("PLAYBOOK_MAX_NON_NEWS", "15"))

# ── OFF_HOURS v5 publish-time funnel & quotas ────────────────────────
_OFF_HOURS_FUNNEL_TOP = int(os.environ.get("PLAYBOOK_FUNNEL_TOP", "100"))
_PLAYBOOK_MIN_NEWS = int(os.environ.get("PLAYBOOK_MIN_NEWS", "5"))

# ── OFF_HOURS v6 correlation & dedup caps ────────────────────────────
_FAMILY_MAP: Dict[str, str] = {"GOOG": "GOOGL", "GOOGL": "GOOG"}
_INDEX_ETFS: frozenset[str] = frozenset({"SPY", "QQQ", "IVV", "VTI", "VOO", "DIA", "IWM"})
_PLAYBOOK_MAX_INDEX_ETF = int(os.environ.get("PLAYBOOK_MAX_INDEX_ETF", "1"))
_PLAYBOOK_CORR_THRESHOLD = float(os.environ.get("PLAYBOOK_CORR_THRESHOLD", "0.85"))
_PLAYBOOK_CORR_CLUSTER_MAX = int(os.environ.get("PLAYBOOK_CORR_CLUSTER_MAX", "2"))
_ALLOW_DOT_SYMBOLS = os.environ.get("ALLOW_DOT_SYMBOLS", "false").lower() in ("1", "true", "yes")

# ── PREMARKET v1 settings ────────────────────────────────────────────
_PREMARKET_PUBLISH_INTERVAL_S = 30.0
_PREMARKET_TOP_N = 10
_PREMARKET_MIN_SNAPSHOTS = 1       # need ≥1 snapshot to score
_PREMARKET_GAP_WEIGHT = 3.0       # gap from playbook entry → high priority
_PREMARKET_VOL_WEIGHT = 2.0       # pre-market volume proxy
_PREMARKET_NEWS_WEIGHT = 2.0      # fresh news
_PREMARKET_SPREAD_WEIGHT = 1.0
_PREMARKET_RSI_WEIGHT = 1.0
# Max possible premarket score
_PREMARKET_MAX_SCORE = (
    3.0 * _PREMARKET_GAP_WEIGHT       # 9
    + 3.0 * _PREMARKET_VOL_WEIGHT     # 6
    + 3.0 * _PREMARKET_NEWS_WEIGHT    # 6
    + 1.0 * _PREMARKET_SPREAD_WEIGHT  # 1
    + 2.0 * _PREMARKET_RSI_WEIGHT     # 2
    + 3.0                              # rvol_points max (Legend Phase 1)
)  # 27.0

# ── Liquidity / universe config ──────────────────────────────────────
_LIQUID_SYMBOLS: frozenset[str] = frozenset(
    s.strip().upper()
    for s in os.environ.get("LIQUID_SYMBOLS", "SPY,QQQ,AAPL,MSFT,NVDA,AMZN,GOOG,GOOGL,META,TSLA").split(",")
    if s.strip()
)
_BASE_SYMBOLS: frozenset[str] = frozenset(
    s.strip().upper()
    for s in os.environ.get("TL_INGEST_SYMBOLS", "SPY,QQQ,AAPL,MSFT,NVDA").split(",")
    if s.strip()
)
_MIN_PRICE = float(os.environ.get("TL_SIG_MIN_PRICE", "3.0"))

# ── Diagnostic mode ────────────────────────────────────────────────────
_DIAGNOSTIC_MODE = os.environ.get("DIAGNOSTIC_MODE", "false").lower() in ("1", "true", "yes")
_DIAG_SNAPSHOT_PATH = Path(settings.data_dir) / "diagnostic_snapshot.json"

# ── News Shock Engine v1 flags ────────────────────────────────────────
_NEWS_SHOCK_ENABLED = os.environ.get("TL_NEWS_SHOCK_ENABLED", "false").lower() in ("1", "true", "yes")
_NEWS_SHOCK_THRESHOLD = int(os.environ.get("TL_NEWS_SHOCK_THRESHOLD", "6"))
_NEWS_BURST_WINDOW_S = float(os.environ.get("TL_NEWS_BURST_WINDOW_S", "600"))
_NEWS_BURST_COUNT = int(os.environ.get("TL_NEWS_BURST_COUNT", "3"))
_NEWS_BURST_FORCE_INCLUDE = os.environ.get("TL_NEWS_BURST_FORCE_INCLUDE", "true").lower() in ("1", "true", "yes")
_NEWS_SHOCK_LOG = os.environ.get("TL_NEWS_SHOCK_LOG", "false").lower() in ("1", "true", "yes")

# ── Legend Phase 2: Consensus boost (signal-side) ────────────────────
_NEWS_CONSENSUS_BOOST_SIG: int = int(os.environ.get("TL_NEWS_CONSENSUS_BOOST", "2"))

# ── RVOL (Legend Phase 1) ────────────────────────────────────────────
_RVOL_ENABLED = os.environ.get("TL_PREMARKET_RVOL_ENABLED", "false").lower() in ("1", "true", "yes")
_RVOL_THRESHOLD = float(os.environ.get("TL_PREMARKET_RVOL_THRESHOLD", "2.0"))
_RVOL_LOOKBACK_BARS = int(os.environ.get("TL_PREMARKET_RVOL_LOOKBACK", "20"))

# Per-symbol cumulative-volume tracking for RVOL
_rvol_cum_volume: Dict[str, Deque[int]] = {}      # symbol → deque of cum_volume per snapshot
_rvol_baseline: Dict[str, float] = {}             # symbol → avg vol-per-bar (rolling 20-bar)

# ── Per-symbol rolling cache ────────────────────────────────────────


@dataclass
class SymbolCache:
    """Rolling window of recent snapshots and news for one symbol."""

    snapshots: Deque[MarketSnapshot] = field(
        default_factory=lambda: deque(maxlen=_CACHE_MAX_SNAPS)
    )
    news: Deque[NewsEvent] = field(
        default_factory=lambda: deque(maxlen=_CACHE_MAX_NEWS)
    )
    last_intent_ts: float = 0.0  # epoch — for cooldown


_cache: Dict[str, SymbolCache] = {}
_lock = threading.Lock()

# ── Runtime state ────────────────────────────────────────────────────
_running = True
_bus = None  # type: Any
_intents_emitted = 0
_snapshots_received = 0
_news_received = 0
_last_forced_intent_ts: float = 0.0
_intent_hourly_ts: Dict[str, list] = {}  # symbol → [epoch, …] of recent intents

# ── Poll-cycle tracking (for diagnostic) ────────────────────────────
_POLL_INTERVAL_S = float(os.environ.get("TL_INGEST_INTERVAL_S", "10"))
_diag_cycle_syms: set = set()         # symbols seen in current cycle window
_diag_cycle_reset_ts: float = 0.0     # epoch when current window opened
_diag_last_cycle_count: int = 0       # count from the most recent completed window

# ── v5 publish-stage diagnostic counters ─────────────────────────────
_diag_hard_pool: int = 0              # symbols passing HARD filters
_diag_relaxed_count: int = 0          # RELAXED subset within funnel top-N
_diag_strict_count: int = 0           # STRICT subset within funnel top-N
_diag_published_count: int = 0        # final published count
_diag_published_news_dist: Dict[str, int] = {}  # news_points bucket → count
_diag_min_news_unmet: bool = False    # True when min-news quota unmet
_diag_quality_dist: Dict[str, int] = {}  # HIGH/MED/LOW → count

# ── v6 publish-stage skip counters ────────────────────────────────────
_diag_skip_family: int = 0
_diag_skip_etf: int = 0
_diag_skip_corr: int = 0

# ── News Shock Engine: per-symbol rolling timestamp window ────────────
_news_ts_by_symbol: Dict[str, Deque[float]] = {}

# ── Category multiplier lookup (Legend Phase 1) ──────────────────────
_CATEGORY_MULTIPLIERS: Dict[str, float] = {
    "FDA": 4.0, "MNA": 3.5, "EARNINGS": 3.0,
    "MGMT": 2.0, "ANALYST": 1.5, "MACRO": 1.0, "GENERAL": 1.0,
}


def _compute_news_impact(headline: str, n_related: int, age_s: float,
                         impact_tags: Optional[List[str]] = None) -> tuple:
    """Score a headline for impact (0-10) and return (score, tags).

    When *impact_tags* contains a known category (from ingest classify_news),
    the raw keyword score is multiplied by the category weight.
    """
    score = 0
    tags: List[str] = []
    hl = headline.lower()

    # analyst words
    if any(w in hl for w in ("upgrade", "downgrade", "maintains", "raises", "lowers", "price target")):
        score += 2
        tags.append("analyst")
    # earnings words
    if any(w in hl for w in ("earnings", "guidance", "beat", "miss", "revenue", "eps")):
        score += 2
        tags.append("earnings")
    # macro words
    if any(w in hl for w in ("fed", "inflation", "rates", "cpi", "jobs", "geopolitics", "war", "nato", "sanctions")):
        score += 2
        tags.append("macro")
    # related tickers
    if n_related >= 3:
        score += 2
        tags.append("multi_ticker")
    elif n_related == 2:
        score += 1
    # freshness
    if age_s <= 300:
        score += 2
        tags.append("fresh_5m")
    elif age_s <= 1800:
        score += 1
        tags.append("fresh_30m")

    # ── Category multiplier (Legend Phase 1) ─────────────────────────
    cat_mult = 1.0
    if impact_tags:
        for tag in impact_tags:
            if tag in _CATEGORY_MULTIPLIERS:
                cat_mult = max(cat_mult, _CATEGORY_MULTIPLIERS[tag])
                if tag not in tags:
                    tags.append(f"cat={tag}")
                break
    if cat_mult > 1.0:
        score = int(score * cat_mult)

    # ── Consensus boost (Legend Phase 2) ─────────────────────────────
    # If ingest tagged CONSENSUS:N, apply additional score boost.
    if impact_tags:
        for tag in impact_tags:
            if tag.startswith("CONSENSUS:"):
                try:
                    n = int(tag.split(":")[1])
                except (IndexError, ValueError):
                    n = 0
                if n >= 2:
                    boost = min(6, (n - 1) * _NEWS_CONSENSUS_BOOST_SIG)
                    score += boost
                    if tag not in tags:
                        tags.append(tag)
                break

    return (min(score, 10), tags)


def _update_burst_flag(symbol: str, now: float) -> bool:
    """Track per-symbol news timestamps and return True if burst detected."""
    dq = _news_ts_by_symbol.setdefault(symbol, deque(maxlen=200))
    dq.append(now)
    # prune older than window
    cutoff = now - _NEWS_BURST_WINDOW_S
    while dq and dq[0] < cutoff:
        dq.popleft()
    return len(dq) >= _NEWS_BURST_COUNT


# ── OFF_HOURS scoring board ─────────────────────────────────────────

@dataclass
class _OffHoursScore:
    """Per-symbol composite score for off-hours ranking.

    Components:
      news_points    0..3   v6 freshness+count model
      momentum_pts   0..3   based on pct_change_3
      vol_points     0..3   stddev of returns (last 20 closes)
      spread_points  0..1   tight spread bonus
      rsi_points     0..2   oversold regime
      liq_points     0..3   liquidity proxy
    """
    # Points
    news_points: float = 0.0
    momentum_pts: float = 0.0
    vol_points: float = 0.0
    spread_points: float = 0.0
    rsi_points: float = 0.0
    liq_points: float = 0.0
    # Metadata
    news_count_2h: int = 0
    momentum_pct: float = 0.0
    vol_pct: float = 0.0
    spread_pct: float = 0.0
    rsi14: Optional[float] = None
    last_price: float = 0.0
    last_update_ts: float = 0.0
    entry_mid: float = 0.0
    invalidation: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    latest_headline: str = ""
    latest_headline_ts: float = 0.0
    # News Shock Engine v1
    impact_score: int = 0
    burst_flag: bool = False

    @property
    def total(self) -> float:
        return (
            self.news_points * _W_NEWS
            + self.liq_points * _W_LIQ
            + self.vol_points * _W_VOL
            + self.momentum_pts * _W_MOM
            + self.spread_points * _W_SPREAD
            + self.rsi_points * _W_RSI
        )


_OFF_HOURS_MAX_SCORE = _OFF_HOURS_MAX_WEIGHTED  # 25.0


_off_hours_board: Dict[str, _OffHoursScore] = {}
_off_hours_board_lock = threading.Lock()


# ── PREMARKET scoring board ──────────────────────────────────────────

@dataclass
class _PremarketScore:
    """Per-symbol premarket composite score.

    Components:
      gap_points     0..3   price gap from playbook entry
      vol_points     0..3   premarket volume/volatility
      news_points    0..3   fresh news in the window
      spread_points  0..1   tight spread bonus
      rsi_points     0..2   RSI regime
      rvol_points    0..3   relative volume (Legend Phase 1)
    """
    gap_points: float = 0.0
    vol_points: float = 0.0
    news_points: float = 0.0
    spread_points: float = 0.0
    rsi_points: float = 0.0
    rvol_points: float = 0.0           # Legend Phase 1
    rvol: float = 0.0                  # raw RVOL ratio
    # Metadata
    gap_pct: float = 0.0           # current price vs playbook entry %
    vol_pct: float = 0.0
    spread_pct: float = 0.0
    rsi14: Optional[float] = None
    news_count_2h: int = 0
    last_price: float = 0.0
    playbook_entry: float = 0.0    # entry price from OFF_HOURS playbook
    playbook_stop: float = 0.0     # stop price from OFF_HOURS playbook
    entry_mid: float = 0.0
    invalidation: float = 0.0
    reason_codes: List[str] = field(default_factory=list)
    latest_headline: str = ""
    latest_headline_ts: float = 0.0
    last_update_ts: float = 0.0
    quality: str = ""
    # News Shock Engine v1
    impact_score: int = 0
    burst_flag: bool = False

    @property
    def total(self) -> float:
        return (
            self.gap_points * _PREMARKET_GAP_WEIGHT
            + self.vol_points * _PREMARKET_VOL_WEIGHT
            + self.news_points * _PREMARKET_NEWS_WEIGHT
            + self.spread_points * _PREMARKET_SPREAD_WEIGHT
            + self.rsi_points * _PREMARKET_RSI_WEIGHT
            + self.rvol_points  # rvol_points already weighted (0-3)
        )


_premarket_board: Dict[str, _PremarketScore] = {}
_premarket_board_lock = threading.Lock()
_premarket_playbook: Dict[str, Dict] = {}    # symbol → playbook draft dict
_premarket_playbook_loaded: bool = False


def _handle_signal(signum, _frame):
    global _running
    log.info("Received shutdown signal (%s)", signum)
    _running = False


# ── Cache helpers ────────────────────────────────────────────────────

def _get_cache(symbol: str) -> SymbolCache:
    """Return (or create) the rolling cache for *symbol*."""
    if symbol not in _cache:
        _cache[symbol] = SymbolCache()
    return _cache[symbol]


def _recent_news_sentiment(sc: SymbolCache) -> Optional[float]:
    """Avg sentiment of news items received in the last ``_NEWS_RECENCY_S``."""
    now = time.time()
    scores: List[float] = []
    for n in reversed(sc.news):
        age = now - n.ts.timestamp() if hasattr(n.ts, "timestamp") else _NEWS_RECENCY_S + 1
        if age > _NEWS_RECENCY_S:
            break
        if n.sentiment is not None:
            scores.append(n.sentiment)
    return (sum(scores) / len(scores)) if scores else None


# ── Hourly-cap helper ──────────────────────────────────────────────

def _hourly_cap_ok(symbol: str, now: float) -> bool:
    """Return True if *symbol* has fewer than *_MAX_INTENTS_PER_SYMBOL_PER_HOUR* intents in the last hour."""
    ts_list = _intent_hourly_ts.setdefault(symbol, [])
    cutoff = now - 3600.0
    # prune old entries
    _intent_hourly_ts[symbol] = [t for t in ts_list if t > cutoff]
    return len(_intent_hourly_ts[symbol]) < _MAX_INTENTS_PER_SYMBOL_PER_HOUR


def _record_intent(symbol: str, now: float) -> None:
    """Record an intent emission timestamp for hourly-cap tracking."""
    _intent_hourly_ts.setdefault(symbol, []).append(now)


# ── Unified intent emitter ─────────────────────────────────────────

def _emit_intent(
    intent: TradeIntent,
    sc: SymbolCache,
    snap: MarketSnapshot,
    now: float,
) -> None:
    """Publish a TradeIntent (or off-hours candidates) and update state."""
    global _intents_emitted

    session = get_us_equity_session()

    rsi_str = f"{snap.rsi14:.1f}" if snap.rsi14 is not None else "n/a"

    # ── OFF_HOURS: accumulate score instead of publishing immediately ──
    if session == OFF_HOURS:
        _update_off_hours_score(intent, sc, snap, now)
        return

    # ── Normal session: publish TradeIntent ──────────────────────────
    log.info(
        "TradeIntent emitted  setup=%s  symbol=%s  last=%.2f  rsi14=%s  "
        "conf=%.2f  entry=[%.2f,%.2f]  inv=%.2f  session=%s  reasons=%s",
        intent.setup_type,
        intent.symbol,
        snap.last,
        rsi_str,
        intent.confidence,
        intent.entry_zone_low,
        intent.entry_zone_high,
        intent.invalidation,
        session,
        intent.reason_codes,
    )

    if _bus is not None:
        _bus.publish(TRADE_INTENT, intent)
    else:
        log.warning("Bus unavailable — intent for %s not published", intent.symbol)

    with _lock:
        sc.last_intent_ts = now
        _intents_emitted += 1
    _record_intent(intent.symbol, now)


# ── OFF_HOURS score accumulation ────────────────────────────────────

def _update_off_hours_score(
    intent: TradeIntent,
    sc: SymbolCache,
    snap: MarketSnapshot,
    now: float,
) -> None:
    """Compute the 6-component score (max 14) and store in the board."""
    sym = snap.symbol
    entry_mid = round((intent.entry_zone_low + intent.entry_zone_high) / 2.0, 2)
    reasons: List[str] = list(intent.reason_codes[:4])

    # ── v5: no hard-filter gating here — score everything ────────────
    # All filtering moved to _publish_off_hours_board()

    # ── 1. News intensity  0..3  (v6 freshness+count model) ──────────
    news_cutoff = now - _OFF_HOURS_NEWS_WINDOW_S
    recent_news = [
        n for n in sc.news
        if hasattr(n.ts, "timestamp") and n.ts.timestamp() > news_cutoff
    ]
    news_count = len(recent_news)

    # Stash latest headline & compute age
    latest_hl = ""
    latest_hl_ts = 0.0
    latest_news_age_min = 999.0
    if recent_news:
        last_n = max(recent_news, key=lambda n: n.ts.timestamp())
        latest_hl = getattr(last_n, "headline", "")[:120]
        latest_hl_ts = last_n.ts.timestamp()
        latest_news_age_min = (now - latest_hl_ts) / 60.0
    elif sc.news:
        last_n = sc.news[-1]
        latest_hl = getattr(last_n, "headline", "")[:120]
        latest_hl_ts = last_n.ts.timestamp() if hasattr(last_n.ts, "timestamp") else 0.0

    # v6 freshness + count scoring
    news_points = 0.0
    if news_count >= 3:
        news_points += 1.0
    if news_count >= 10:
        news_points += 1.0
    if latest_news_age_min <= 30.0:
        news_points += 1.0
    news_points = min(3.0, news_points)
    if news_count > 0:
        reasons.append(f"n2h={news_count} age={latest_news_age_min:.0f}m pts={news_points:.0f}")

    # ── 2. Momentum  0..3  (pct_change across 3 bars) ───────────────
    momentum_pts = 0.0
    pct_change_3 = 0.0
    if len(sc.snapshots) >= 3:
        c0 = sc.snapshots[-1].last
        c2 = sc.snapshots[-3].last
        if c2 > 0:
            pct_change_3 = (c0 - c2) / c2 * 100.0
            if pct_change_3 >= 0.25:
                momentum_pts = 3.0
            elif pct_change_3 >= 0.10:
                momentum_pts = 2.0
            elif pct_change_3 > 0:
                momentum_pts = 1.0
            if momentum_pts > 0:
                reasons.append(f"mom3={pct_change_3:+.3f}%")

    # ── 3. Volatility  0..3  (stddev of returns, last 20 closes) ────
    vol_points = 0.0
    vol_pct = 0.0
    low_conf_vol = False
    closes_for_vol = [s.last for s in list(sc.snapshots)[-20:]]
    if len(closes_for_vol) >= 3:  # need >= 3 closes for >= 2 returns (stdev needs >= 2)
        rets = [(closes_for_vol[i] / closes_for_vol[i - 1] - 1.0)
                for i in range(1, len(closes_for_vol))
                if closes_for_vol[i - 1] > 0]
        if len(rets) >= 2:
            vol_pct = statistics.stdev(rets) * 100.0  # in percentage
            if len(closes_for_vol) < 20:
                low_conf_vol = True
            if vol_pct >= 0.25:
                vol_points = 3.0
            elif vol_pct >= 0.12:
                vol_points = 2.0
            elif vol_pct >= 0.05:
                vol_points = 1.0
            reasons.append(f"vol={vol_pct:.3f}%{'*' if low_conf_vol else ''}")

    # ── 4. Spread quality  0..1 ──────────────────────────────────────
    spread_points = 0.0
    spread_pct = 0.0
    if snap.bid > 0 and snap.ask > 0 and snap.last > 0:
        spread_pct = (snap.ask - snap.bid) / snap.last
        if spread_pct <= _SPREAD_MAX_PCT:
            spread_points = 1.0
        reasons.append(f"spread={spread_pct:.4f}")

    # ── 5. RSI regime  0..2 ──────────────────────────────────────────
    rsi_points = 0.0
    if snap.rsi14 is not None:
        if snap.rsi14 <= _RSI_THRESHOLD:
            rsi_points = 2.0
        elif snap.rsi14 <= _RSI_THRESHOLD + 10:
            rsi_points = 1.0
        reasons.append(f"rsi={snap.rsi14:.1f}")

    # ── 6. Liquidity proxy  0..3  (v5: news_count_2h > 0 → liq=1) ───
    liq_points = 0.0
    if sym in _LIQUID_SYMBOLS:
        liq_points = 3.0
    elif sym in _BASE_SYMBOLS:
        liq_points = 2.0
    elif news_count > 0:
        liq_points = 1.0
    if liq_points > 0:
        reasons.append(f"liq={liq_points:.0f}")

    # ── v2 weighted reason codes ─────────────────────────────────────
    weighted_reasons = [
        f"news_pts={news_points:.0f} w={_W_NEWS:.0f} => +{news_points * _W_NEWS:.0f}",
        f"liq_pts={liq_points:.0f} w={_W_LIQ:.0f} => +{liq_points * _W_LIQ:.0f}",
        f"vol_pts={vol_points:.0f} w={_W_VOL:.0f} => +{vol_points * _W_VOL:.0f}",
        f"mom_pts={momentum_pts:.0f} w={_W_MOM:.0f} => +{momentum_pts * _W_MOM:.0f}",
    ]
    reasons.extend(weighted_reasons)

    # ── News Shock Engine: aggregate impact + burst from cached news ──
    sym_impact = 0
    sym_burst = False
    if _NEWS_SHOCK_ENABLED:
        for n in recent_news:
            sym_impact = max(sym_impact, getattr(n, "impact_score", 0))
        sym_burst = any(getattr(n, "burst_flag", False) for n in recent_news)
        # Also check the rolling ts window
        dq = _news_ts_by_symbol.get(sym)
        if dq and len(dq) >= _NEWS_BURST_COUNT:
            sym_burst = True

    score = _OffHoursScore(
        news_points=news_points,
        momentum_pts=momentum_pts,
        vol_points=vol_points,
        spread_points=spread_points,
        rsi_points=rsi_points,
        liq_points=liq_points,
        news_count_2h=news_count,
        momentum_pct=round(pct_change_3, 4),
        vol_pct=round(vol_pct, 4),
        spread_pct=round(spread_pct, 6),
        rsi14=snap.rsi14,
        last_price=snap.last,
        last_update_ts=now,
        entry_mid=entry_mid,
        invalidation=round(intent.invalidation, 2),
        reason_codes=reasons[:8],
        latest_headline=latest_hl,
        latest_headline_ts=latest_hl_ts,
        impact_score=sym_impact,
        burst_flag=sym_burst,
    )

    with _off_hours_board_lock:
        _off_hours_board[snap.symbol] = score


def _pearson(xs: List[float], ys: List[float]) -> float:
    """Pearson correlation of two aligned series (last *n* elements)."""
    n = min(len(xs), len(ys))
    if n < 3:
        return 0.0
    xs, ys = xs[-n:], ys[-n:]
    mx = sum(xs) / n
    my = sum(ys) / n
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sx = math.sqrt(max(0, sum((x - mx) ** 2 for x in xs)))
    sy = math.sqrt(max(0, sum((y - my) ** 2 for y in ys)))
    if sx == 0 or sy == 0:
        return 0.0
    return round(cov / (sx * sy), 3)


def _publish_off_hours_board() -> None:
    """Publish the top-N off-hours candidates as WatchCandidate + OpenPlanCandidate.

    v6 publish-time funnel:
    1. HARD filter all board entries (spread, price, dot/dash symbols)
    2. Take top FUNNEL_TOP (100) by weighted_total
    3. Classify: STRICT (liq>=2 OR news>=2), RELAXED (liq>=1 OR news>=1)
    4. Fill slots with family dedup, ETF cap, and correlation cap:
       - GOOG/GOOGL family: only one allowed
       - Index ETFs (SPY,QQQ,IVV,VTI,VOO,DIA,IWM): max PLAYBOOK_MAX_INDEX_ETF
       - Correlation cluster: max PLAYBOOK_CORR_CLUSTER_MAX per cluster
    5. News quota: prioritise news-driven if enough STRICT news candidates
    """
    global _intents_emitted
    global _diag_hard_pool, _diag_relaxed_count, _diag_strict_count
    global _diag_published_count, _diag_published_news_dist
    global _diag_min_news_unmet, _diag_quality_dist
    global _diag_skip_family, _diag_skip_etf, _diag_skip_corr

    session = get_us_equity_session()
    if session != OFF_HOURS:
        return

    with _off_hours_board_lock:
        if not _off_hours_board:
            return
        board_items = list(_off_hours_board.items())

    # ── Build close-lists for correlation (under cache lock) ─────────
    with _lock:
        close_lists: Dict[str, List[float]] = {}
        for sym, sc in _cache.items():
            if len(sc.snapshots) >= 2:
                close_lists[sym] = [s.last for s in list(sc.snapshots)[-20:]]

    # ── Stage 1: HARD filters ────────────────────────────────────────
    hard_pool: list = []
    for sym, sc in board_items:
        if sc.spread_points < 1:
            continue  # spread too wide
        if sc.last_price < _MIN_PRICE or sc.last_price > _OFF_HOURS_MAX_PRICE:
            continue  # price out of range
        if ("." in sym or "-" in sym) and not _ALLOW_DOT_SYMBOLS and sym not in _LIQUID_SYMBOLS:
            continue  # warrants / units
        hard_pool.append((sym, sc))

    # Sort: weighted total desc → news_count desc → recency desc
    hard_pool.sort(key=lambda x: (-x[1].total, -x[1].news_count_2h, -x[1].last_update_ts))
    _diag_hard_pool = len(hard_pool)

    # ── Stage 2: Top FUNNEL_TOP from HARD pool ───────────────────────
    funnel = hard_pool[:_OFF_HOURS_FUNNEL_TOP]

    # ── Stage 3: Classify STRICT / RELAXED within funnel ─────────────
    # News Shock: treat burst/shock as STRICT regardless of liq/news_points
    def _is_shock_or_burst(sc) -> bool:
        return _NEWS_SHOCK_ENABLED and (
            sc.burst_flag or sc.impact_score >= _NEWS_SHOCK_THRESHOLD
        )

    strict_list = [
        (s, sc) for s, sc in funnel
        if sc.liq_points >= 2 or sc.news_points >= 2 or _is_shock_or_burst(sc)
    ]
    relaxed_list = [
        (s, sc) for s, sc in funnel
        if (sc.liq_points >= 1 or sc.news_points >= 1)
        and not (sc.liq_points >= 2 or sc.news_points >= 2 or _is_shock_or_burst(sc))
    ]
    _diag_strict_count = len(strict_list)
    _diag_relaxed_count = len(relaxed_list)

    # ── Stage 4: Fill publish slots with v6 dedup + corr caps ────────
    base_set = set(_BASE_SYMBOLS)
    prefix_counts: Dict[str, int] = {}
    base_count = 0
    non_news_count = 0
    etf_count = 0
    family_selected: Set[str] = set()   # normalized family keys already taken
    top_n: list = []                     # (sym, sc, quality)
    quality_dist: Dict[str, int] = {"HIGH": 0, "MED": 0, "LOW": 0}

    # v6 skip counters
    _skip_family = 0
    _skip_etf = 0
    _skip_corr = 0

    # Correlation cluster tracking: list of selected sym close-lists
    selected_closes: List[tuple] = []    # [(sym, closes), ...]

    def _corr_ok(sym: str) -> bool:
        """Check if *sym* fits within correlation cluster limits."""
        if sym not in close_lists:
            return True  # no data → allow
        cand_closes = close_lists[sym]
        # Count how many already-selected symbols are highly correlated
        cluster_count = 0
        for sel_sym, sel_closes in selected_closes:
            r = _pearson(cand_closes, sel_closes)
            if r >= _PLAYBOOK_CORR_THRESHOLD:
                cluster_count += 1
        return cluster_count < _PLAYBOOK_CORR_CLUSTER_MAX

    def _can_add(sym: str, sc) -> Optional[str]:
        """Return None if OK, or a skip reason string."""
        nonlocal base_count, non_news_count, etf_count
        prefix = sym[:2] if len(sym) >= 2 else sym
        if prefix_counts.get(prefix, 0) >= _PLAYBOOK_MAX_PER_PREFIX:
            return "prefix"
        if sym in base_set and base_count >= _PLAYBOOK_MAX_BASE:
            return "base"
        # News Shock: burst/shock bypasses non_news cap
        shock_bypass = _NEWS_BURST_FORCE_INCLUDE and _is_shock_or_burst(sc)
        if sc.news_points == 0 and non_news_count >= _PLAYBOOK_MAX_NON_NEWS and not shock_bypass:
            return "non_news"
        # v6: Family dedup (GOOG/GOOGL)
        if sym in _FAMILY_MAP:
            family_key = min(sym, _FAMILY_MAP[sym])  # normalized key
            if family_key in family_selected:
                return "family"
        # v6: Index ETF cap
        if sym in _INDEX_ETFS and etf_count >= _PLAYBOOK_MAX_INDEX_ETF:
            return "etf"
        # v6: Correlation cap
        if not _corr_ok(sym):
            return "corr"
        return None

    def _add(sym: str, sc, quality: str) -> None:
        nonlocal base_count, non_news_count, etf_count
        prefix = sym[:2] if len(sym) >= 2 else sym
        prefix_counts[prefix] = prefix_counts.get(prefix, 0) + 1
        if sym in base_set:
            base_count += 1
        if sc.news_points == 0:
            non_news_count += 1
        if sym in _INDEX_ETFS:
            etf_count += 1
        if sym in _FAMILY_MAP:
            family_selected.add(min(sym, _FAMILY_MAP[sym]))
        if sym in close_lists:
            selected_closes.append((sym, close_lists[sym]))
        top_n.append((sym, sc, quality))
        quality_dist[quality] = quality_dist.get(quality, 0) + 1

    def _try_add(sym: str, sc, quality: str) -> bool:
        nonlocal _skip_family, _skip_etf, _skip_corr
        reason = _can_add(sym, sc)
        if reason is None:
            _add(sym, sc, quality)
            return True
        if reason == "family":
            _skip_family += 1
        elif reason == "etf":
            _skip_etf += 1
        elif reason == "corr":
            _skip_corr += 1
        return False

    # News quota check: only enforce if enough strict news candidates
    strict_news_cands = [(s, sc) for s, sc in strict_list if sc.news_points >= 2]
    min_news_unmet = len(strict_news_cands) < _PLAYBOOK_MIN_NEWS
    _diag_min_news_unmet = min_news_unmet
    if min_news_unmet:
        log.info(
            "min_news_unmet reason=insufficient_news_candidates "
            "available=%d required=%d",
            len(strict_news_cands), _PLAYBOOK_MIN_NEWS,
        )

    # Pass 1a: If quota met, prioritise news-driven STRICT first
    news_selected = 0
    if not min_news_unmet:
        for sym, sc in strict_news_cands:
            if len(top_n) >= _OFF_HOURS_TOP_N:
                break
            if news_selected >= _PLAYBOOK_MIN_NEWS:
                break
            if sym in {s for s, _, _ in top_n}:
                continue
            _try_add(sym, sc, "HIGH")
            if (sym, sc, "HIGH") in top_n[-1:] if top_n else False:
                news_selected += 1

    # Pass 1b: Fill with remaining STRICT candidates
    selected_syms = {sym for sym, _, _ in top_n}
    for sym, sc in strict_list:
        if len(top_n) >= _OFF_HOURS_TOP_N:
            break
        if sym in selected_syms:
            continue
        _try_add(sym, sc, "HIGH")

    # Pass 2: Fill with RELAXED candidates
    selected_syms = {sym for sym, _, _ in top_n}
    for sym, sc in relaxed_list:
        if len(top_n) >= _OFF_HOURS_TOP_N:
            break
        if sym in selected_syms:
            continue
        _try_add(sym, sc, "MED")

    # Pass 3: Fill with remaining HARD pool (low_quality)
    selected_syms = {sym for sym, _, _ in top_n}
    for sym, sc in hard_pool:
        if len(top_n) >= _OFF_HOURS_TOP_N:
            break
        if sym in selected_syms:
            continue
        _try_add(sym, sc, "LOW")

    # ── Track diagnostic counters ────────────────────────────────────
    _diag_quality_dist = quality_dist
    _diag_skip_family = _skip_family
    _diag_skip_etf = _skip_etf
    _diag_skip_corr = _skip_corr
    news_dist: Dict[str, int] = {}
    for _, sc, _ in top_n:
        k = str(int(sc.news_points))
        news_dist[k] = news_dist.get(k, 0) + 1
    _diag_published_news_dist = news_dist

    published = 0
    for sym, sc, quality in top_n:
        conf = round(sc.total / _OFF_HOURS_MAX_SCORE, 3)
        # Append quality + low_quality marker to reason codes
        pub_reasons = list(sc.reason_codes[:6])
        pub_reasons.append(f"quality={quality}")
        if quality == "LOW":
            pub_reasons.append("low_quality")
        watch = WatchCandidate(
            symbol=sym,
            score=sc.total,
            reason_codes=pub_reasons[:8],
            session=session,
            news_count_2h=sc.news_count_2h,
            latest_headline=sc.latest_headline[:120],
            news_points=sc.news_points,
            momentum_pts=sc.momentum_pts,
            vol_points=sc.vol_points,
            spread_points=sc.spread_points,
            rsi_points=sc.rsi_points,
            liq_points=sc.liq_points,
            total_score=sc.total,
            quality=quality,
        )
        plan_cand = OpenPlanCandidate(
            symbol=sym,
            suggested_entry=sc.entry_mid,
            suggested_stop=sc.invalidation,
            confidence=conf,
            session=session,
            reason_codes=pub_reasons[:8],
            news_count_2h=sc.news_count_2h,
            latest_headline=sc.latest_headline[:120],
            vol_pct=sc.vol_pct,
            news_points=sc.news_points,
            momentum_pts=sc.momentum_pts,
            vol_points=sc.vol_points,
            spread_points=sc.spread_points,
            rsi_points=sc.rsi_points,
            liq_points=sc.liq_points,
            total_score=sc.total,
            quality=quality,
            impact_score=sc.impact_score,
            burst_flag=sc.burst_flag,
        )
        if _bus is not None:
            _bus.publish(WATCH_CANDIDATE, watch)
            _bus.publish(OPEN_PLAN_CANDIDATE, plan_cand)
            published += 1

    _diag_published_count = published

    with _lock:
        _intents_emitted += published

    log.info(
        "OFF_HOURS board published  topN=%d  hard_pool=%d  strict=%d  "
        "relaxed=%d  news_selected=%d  min_news_unmet=%s  q=%s  "
        "v6_skip: family=%d etf=%d corr=%d",
        published, len(hard_pool), len(strict_list), len(relaxed_list),
        news_selected, min_news_unmet, quality_dist,
        _skip_family, _skip_etf, _skip_corr,
    )

    # Log the top 10 entries with full breakdown
    for i, (sym, sc, quality) in enumerate(top_n[:10], 1):
        log.info(
            "  #%d %-6s  total=%4.0f  q=%s  news=%d(%.0f) mom=%.0f vol=%.0f sprd=%.0f "
            "rsi=%.0f liq=%.0f  entry=%.2f  stop=%.2f  hl=%s",
            i, sym, sc.total, quality, sc.news_count_2h, sc.news_points,
            sc.momentum_pts, sc.vol_points, sc.spread_points,
            sc.rsi_points, sc.liq_points,
            sc.entry_mid, sc.invalidation,
            sc.latest_headline[:40] if sc.latest_headline else "-",
        )


# ── PREMARKET scoring accumulation ──────────────────────────────────

def _load_premarket_playbook() -> None:
    """Load OFF_HOURS playbook drafts into the premarket reference dict."""
    global _premarket_playbook_loaded, _premarket_playbook
    if _premarket_playbook_loaded:
        return
    drafts = load_playbook_drafts()
    _premarket_playbook = {d["symbol"]: d for d in drafts if "symbol" in d}
    _premarket_playbook_loaded = True
    log.info(
        "Premarket playbook loaded: %d symbols  top5=%s",
        len(_premarket_playbook),
        sorted(_premarket_playbook.keys())[:5],
    )


def _update_premarket_score(snap: MarketSnapshot, sc: SymbolCache, now: float) -> None:
    """Compute premarket score for *snap.symbol* and store in the board.

    Scoring components:
    1. Gap points (0..3):  how much price has moved from playbook entry
    2. Vol points (0..3):  premarket volatility (stddev of recent returns)
    3. News points (0..3): fresh news freshness + count (same as v6)
    4. Spread points (0..1): tight spread bonus
    5. RSI points (0..2):  oversold regime bonus
    6. RVOL points (0..3): relative volume (Legend Phase 1; gated by flag)
    """
    sym = snap.symbol
    reasons: List[str] = []

    # ── 1. Gap from playbook entry  0..3 ─────────────────────────────
    gap_points = 0.0
    gap_pct = 0.0
    pb = _premarket_playbook.get(sym)
    pb_entry = pb.get("entry", 0.0) if pb else 0.0
    pb_stop = pb.get("stop", 0.0) if pb else 0.0
    if pb_entry > 0 and snap.last > 0:
        gap_pct = (snap.last - pb_entry) / pb_entry * 100.0
        abs_gap = abs(gap_pct)
        if abs_gap >= 2.0:
            gap_points = 3.0
        elif abs_gap >= 1.0:
            gap_points = 2.0
        elif abs_gap >= 0.3:
            gap_points = 1.0
        reasons.append(f"gap={gap_pct:+.2f}%")
    elif snap.last > 0:
        # No playbook entry — use momentum as gap proxy
        if len(sc.snapshots) >= 3:
            first = sc.snapshots[0].last
            if first > 0:
                gap_pct = (snap.last - first) / first * 100.0
                abs_gap = abs(gap_pct)
                if abs_gap >= 1.5:
                    gap_points = 2.0
                elif abs_gap >= 0.5:
                    gap_points = 1.0
                reasons.append(f"pre_mom={gap_pct:+.2f}%")

    # ── 2. Volatility  0..3 ─────────────────────────────────────────
    vol_points = 0.0
    vol_pct = 0.0
    closes = [s.last for s in list(sc.snapshots)[-20:]]
    if len(closes) >= 3:
        rets = [
            (closes[i] / closes[i - 1] - 1.0)
            for i in range(1, len(closes))
            if closes[i - 1] > 0
        ]
        if len(rets) >= 2:
            vol_pct = statistics.stdev(rets) * 100.0
            if vol_pct >= 0.25:
                vol_points = 3.0
            elif vol_pct >= 0.12:
                vol_points = 2.0
            elif vol_pct >= 0.05:
                vol_points = 1.0
            reasons.append(f"vol={vol_pct:.3f}%")

    # ── 3. News  0..3  (v6 freshness+count model) ───────────────────
    news_cutoff = now - _OFF_HOURS_NEWS_WINDOW_S
    recent_news = [
        n for n in sc.news
        if hasattr(n.ts, "timestamp") and n.ts.timestamp() > news_cutoff
    ]
    news_count = len(recent_news)
    latest_hl = ""
    latest_hl_ts = 0.0
    latest_news_age_min = 999.0
    if recent_news:
        last_n = max(recent_news, key=lambda n: n.ts.timestamp())
        latest_hl = getattr(last_n, "headline", "")[:120]
        latest_hl_ts = last_n.ts.timestamp()
        latest_news_age_min = (now - latest_hl_ts) / 60.0
    elif sc.news:
        last_n = sc.news[-1]
        latest_hl = getattr(last_n, "headline", "")[:120]
        latest_hl_ts = last_n.ts.timestamp() if hasattr(last_n.ts, "timestamp") else 0.0

    news_points = 0.0
    if news_count >= 3:
        news_points += 1.0
    if news_count >= 10:
        news_points += 1.0
    if latest_news_age_min <= 30.0:
        news_points += 1.0
    news_points = min(3.0, news_points)
    if news_count > 0:
        reasons.append(f"n2h={news_count} age={latest_news_age_min:.0f}m pts={news_points:.0f}")

    # ── 4. Spread quality  0..1 ──────────────────────────────────────
    spread_points = 0.0
    spread_pct = 0.0
    if snap.bid > 0 and snap.ask > 0 and snap.last > 0:
        spread_pct = (snap.ask - snap.bid) / snap.last
        if spread_pct <= _SPREAD_MAX_PCT:
            spread_points = 1.0
        reasons.append(f"spread={spread_pct:.4f}")

    # ── 5. RSI regime  0..2 ──────────────────────────────────────────
    rsi_points = 0.0
    if snap.rsi14 is not None:
        if snap.rsi14 <= _RSI_THRESHOLD:
            rsi_points = 2.0
        elif snap.rsi14 <= _RSI_THRESHOLD + 10:
            rsi_points = 1.0
        reasons.append(f"rsi={snap.rsi14:.1f}")

    # ── 6. RVOL  0..3  (Legend Phase 1; gated by TL_PREMARKET_RVOL_ENABLED) ──
    rvol_points = 0.0
    rvol_ratio = 0.0
    if _RVOL_ENABLED:
        cum_vol = getattr(snap, "cum_volume", 0) or snap.volume
        dq = _rvol_cum_volume.setdefault(sym, deque(maxlen=_RVOL_LOOKBACK_BARS))
        dq.append(cum_vol)
        if len(dq) >= 2:
            # Compute per-bar volume deltas
            deltas = [dq[i] - dq[i - 1] for i in range(1, len(dq)) if dq[i] > dq[i - 1]]
            if deltas:
                avg_per_bar = sum(deltas) / len(deltas)
                # Baseline: rolling average (or historical if available)
                baseline = _rvol_baseline.get(sym, avg_per_bar)
                _rvol_baseline[sym] = baseline * 0.95 + avg_per_bar * 0.05  # EMA update
                if baseline > 0:
                    rvol_ratio = avg_per_bar / baseline
                    if rvol_ratio >= 5.0:
                        rvol_points = 3.0
                    elif rvol_ratio >= _RVOL_THRESHOLD:
                        rvol_points = 2.0
                    elif rvol_ratio >= 1.2:
                        rvol_points = 1.0
                    reasons.append(f"rvol={rvol_ratio:.1f}x")

    # ── Quality classification ───────────────────────────────────────
    total = (
        gap_points * _PREMARKET_GAP_WEIGHT
        + vol_points * _PREMARKET_VOL_WEIGHT
        + news_points * _PREMARKET_NEWS_WEIGHT
        + spread_points * _PREMARKET_SPREAD_WEIGHT
        + rsi_points * _PREMARKET_RSI_WEIGHT
        + rvol_points
    )
    if gap_points >= 2 and (news_points >= 1 or vol_points >= 2):
        quality = "HIGH"
    elif gap_points >= 1 or news_points >= 2 or rvol_points >= 2:
        quality = "MED"
    else:
        quality = "LOW"

    entry_mid = round((snap.bid + snap.ask) / 2.0, 2) if snap.bid > 0 and snap.ask > 0 else snap.last
    inv = pb_stop if pb_stop > 0 else round(snap.last * 0.995, 2)

    # ── News Shock: aggregate impact + burst for premarket ────────────
    sym_impact = 0
    sym_burst = False
    if _NEWS_SHOCK_ENABLED:
        for n in recent_news:
            sym_impact = max(sym_impact, getattr(n, "impact_score", 0))
        sym_burst = any(getattr(n, "burst_flag", False) for n in recent_news)
        dq = _news_ts_by_symbol.get(sym)
        if dq and len(dq) >= _NEWS_BURST_COUNT:
            sym_burst = True

    score = _PremarketScore(
        gap_points=gap_points,
        vol_points=vol_points,
        news_points=news_points,
        spread_points=spread_points,
        rsi_points=rsi_points,
        rvol_points=rvol_points,
        rvol=round(rvol_ratio, 2),
        gap_pct=round(gap_pct, 4),
        vol_pct=round(vol_pct, 4),
        spread_pct=round(spread_pct, 6),
        rsi14=snap.rsi14,
        news_count_2h=news_count,
        last_price=snap.last,
        playbook_entry=pb_entry,
        playbook_stop=pb_stop,
        entry_mid=entry_mid,
        invalidation=round(inv, 2),
        reason_codes=reasons[:8],
        latest_headline=latest_hl,
        latest_headline_ts=latest_hl_ts,
        last_update_ts=now,
        quality=quality,
        impact_score=sym_impact,
        burst_flag=sym_burst,
    )

    with _premarket_board_lock:
        _premarket_board[snap.symbol] = score


def _publish_premarket_board() -> None:
    """Publish the top PREMARKET candidates as OpenPlanCandidate.

    Simpler than the OFF_HOURS funnel: scores all symbols with ≥1 snapshot,
    applies HARD filters (spread, price), sorts by total desc, takes top N.
    """
    global _intents_emitted

    session = get_us_equity_session()
    if session != PREMARKET:
        return

    with _premarket_board_lock:
        if not _premarket_board:
            return
        board_items = list(_premarket_board.items())

    # ── HARD filters ─────────────────────────────────────────────────
    filtered: list = []
    for sym, sc in board_items:
        if sc.last_price < _MIN_PRICE or sc.last_price > _OFF_HOURS_MAX_PRICE:
            continue
        if ("." in sym or "-" in sym) and not _ALLOW_DOT_SYMBOLS and sym not in _LIQUID_SYMBOLS:
            continue
        filtered.append((sym, sc))

    # Sort by total desc → gap_pct abs desc → news desc
    filtered.sort(key=lambda x: (-x[1].total, -abs(x[1].gap_pct), -x[1].news_count_2h))

    # ── News Shock: prepend shock candidates to ensure they enter top_n ──
    if _NEWS_SHOCK_ENABLED:
        shock_syms: set = set()
        shock_cands: list = []
        for sym, sc in filtered:
            if sc.impact_score >= _NEWS_SHOCK_THRESHOLD or sc.burst_flag:
                shock_cands.append((sym, sc))
                shock_syms.add(sym)
        non_shock = [(s, sc) for s, sc in filtered if s not in shock_syms]
        filtered = shock_cands + non_shock

    top_n = filtered[:_PREMARKET_TOP_N]

    published = 0
    for sym, sc in top_n:
        conf = round(sc.total / _PREMARKET_MAX_SCORE, 3) if _PREMARKET_MAX_SCORE > 0 else 0.0

        pub_reasons = list(sc.reason_codes[:6])
        pub_reasons.append(f"quality={sc.quality}")
        if sc.playbook_entry > 0:
            pub_reasons.append(f"pb_entry={sc.playbook_entry:.2f}")
        if sc.rvol > 0:
            pub_reasons.append(f"rvol={sc.rvol:.1f}x")

        plan_cand = OpenPlanCandidate(
            symbol=sym,
            suggested_entry=sc.entry_mid,
            suggested_stop=sc.invalidation,
            confidence=conf,
            session="PREMARKET",
            reason_codes=pub_reasons[:8],
            news_count_2h=sc.news_count_2h,
            latest_headline=sc.latest_headline[:120],
            vol_pct=sc.vol_pct,
            news_points=sc.news_points,
            momentum_pts=sc.gap_points,  # reuse momentum slot for gap
            vol_points=sc.vol_points,
            spread_points=sc.spread_points,
            rsi_points=sc.rsi_points,
            liq_points=0.0,  # not used in premarket; gap replaces liq
            total_score=sc.total,
            quality=sc.quality,
            impact_score=sc.impact_score,
            burst_flag=sc.burst_flag,
        )

        # Also publish a WatchCandidate for the monitor board
        watch = WatchCandidate(
            symbol=sym,
            score=sc.total,
            reason_codes=pub_reasons[:8],
            session="PREMARKET",
            news_count_2h=sc.news_count_2h,
            latest_headline=sc.latest_headline[:120],
            news_points=sc.news_points,
            momentum_pts=sc.gap_points,
            vol_points=sc.vol_points,
            spread_points=sc.spread_points,
            rsi_points=sc.rsi_points,
            liq_points=0.0,
            total_score=sc.total,
            quality=sc.quality,
        )

        if _bus is not None:
            _bus.publish(WATCH_CANDIDATE, watch)
            _bus.publish(OPEN_PLAN_CANDIDATE, plan_cand)
            published += 1

    with _lock:
        _intents_emitted += published

    log.info(
        "PREMARKET board published  topN=%d/%d  filtered=%d  "
        "playbook_syms=%d  board_size=%d",
        published, _PREMARKET_TOP_N, len(filtered),
        len(_premarket_playbook), len(board_items),
    )

    for i, (sym, sc) in enumerate(top_n[:10], 1):
        log.info(
            "  #%d %-6s  total=%4.0f  q=%s  gap=%+.2f%%  news=%d(%.0f) "
            "vol=%.0f sprd=%.0f rsi=%.0f  entry=%.2f  stop=%.2f  pb_entry=%.2f",
            i, sym, sc.total, sc.quality, sc.gap_pct,
            sc.news_count_2h, sc.news_points,
            sc.vol_points, sc.spread_points, sc.rsi_points,
            sc.entry_mid, sc.invalidation, sc.playbook_entry,
        )


# ── Diagnostic snapshot ──────────────────────────────────────────────

def _print_diagnostic() -> None:
    """Print and persist a structured diagnostic snapshot of OFF_HOURS state.

    Only runs when ``DIAGNOSTIC_MODE=true`` and session is OFF_HOURS.
    """
    session = get_us_equity_session()
    if session != OFF_HOURS:
        return

    now = time.time()
    news_cutoff = now - _OFF_HOURS_NEWS_WINDOW_S

    # ── Gather board + cache data under locks ────────────────────────
    with _off_hours_board_lock:
        board_copy = dict(_off_hours_board)  # sym → _OffHoursScore
    with _lock:
        cache_syms = set(_cache.keys())
        # Collect close lists for correlation
        close_lists: Dict[str, List[float]] = {}
        rsi_available_count = 0
        news_2h_count = 0
        total_news_cached = 0
        news_ts_datetime_count = 0
        news_unique_syms: Set[str] = set()
        news_count_by_sym: Dict[str, int] = {}  # sym → news_count_2h
        for sym, sc in _cache.items():
            if sc.snapshots and sc.snapshots[-1].rsi14 is not None:
                rsi_available_count += 1
            sym_news_total = 0
            for n in sc.news:
                total_news_cached += 1
                if hasattr(n.ts, "timestamp"):
                    news_ts_datetime_count += 1
            n_count = sum(
                1 for n in sc.news
                if hasattr(n.ts, "timestamp") and n.ts.timestamp() > news_cutoff
            )
            if n_count > 0:
                news_2h_count += 1
                news_unique_syms.add(sym)
                news_count_by_sym[sym] = n_count
            # Last 20 closes for correlation
            if len(sc.snapshots) >= 2:
                close_lists[sym] = [s.last for s in list(sc.snapshots)[-20:]]

    universe_size = len(cache_syms)
    liq_pass = sum(1 for s in cache_syms if s in _LIQUID_SYMBOLS or s in _BASE_SYMBOLS)
    high_score = sum(1 for sc in board_copy.values() if sc.total >= 10)  # v2: >=10 weighted

    # ── 1) Universe Overview ─────────────────────────────────────────
    diag: Dict[str, Any] = {}
    diag["ts"] = datetime.now(timezone.utc).isoformat()
    diag["session"] = session

    # Average vol_pct across on-board symbols
    vol_pcts = [sc.vol_pct for sc in board_copy.values() if sc.vol_pct > 0]
    avg_vol_pct = round(sum(vol_pcts) / len(vol_pcts), 4) if vol_pcts else 0.0

    # Top 10 symbols by news_count_2h
    top10_news_syms = sorted(news_count_by_sym.items(), key=lambda x: x[1], reverse=True)[:10]

    overview = {
        "universe_size": universe_size,
        "symbols_polled_last_cycle": _diag_last_cycle_count,
        "count_with_rsi_available": rsi_available_count,
        "count_with_news_last_2h": news_2h_count,
        "news_cache_unique_symbols": len(news_unique_syms),
        "news_cache_total_events": total_news_cached,
        "news_cache_ts_is_datetime": news_ts_datetime_count,
        "count_passing_liquidity_filter": liq_pass,
        "pct_from_liquid": round(liq_pass / universe_size * 100, 1) if universe_size else 0.0,
        "count_on_board_gated": len(board_copy),
        "count_scored_total_gte_10": high_score,
        "max_weighted_total": _OFF_HOURS_MAX_SCORE,
        "avg_vol_pct": avg_vol_pct,
        # v5 funnel stats
        "v5_hard_pool": _diag_hard_pool,
        "v5_relaxed_count": _diag_relaxed_count,
        "v5_strict_count": _diag_strict_count,
        "v5_published": _diag_published_count,
        "v5_min_news_unmet": _diag_min_news_unmet,
        "v5_quality_dist": dict(_diag_quality_dist),
        "v5_published_news_dist": dict(_diag_published_news_dist),
        "top10_by_news_count_2h": [
            {"symbol": s, "news_count_2h": c} for s, c in top10_news_syms
        ],
    }
    diag["universe_overview"] = overview

    # ── 2) Score Distribution ────────────────────────────────────────
    max_bucket = int(_OFF_HOURS_MAX_SCORE) + 1
    histogram: Dict[str, int] = {str(i): 0 for i in range(max_bucket)}
    for sc in board_copy.values():
        bucket = str(min(max_bucket - 1, int(sc.total)))
        histogram[bucket] = histogram.get(bucket, 0) + 1

    sorted_items = sorted(board_copy.items(), key=lambda x: x[1].total, reverse=True)

    def _top10_by(key_fn):
        ranked = sorted(board_copy.items(), key=lambda x: key_fn(x[1]), reverse=True)
        return [{"symbol": s, "value": round(key_fn(v), 2)} for s, v in ranked[:10]]

    score_dist = {
        "histogram": histogram,
        "top10_by_news": _top10_by(lambda s: s.news_points),
        "top10_by_momentum": _top10_by(lambda s: s.momentum_pts),
        "top10_by_volatility": _top10_by(lambda s: s.vol_points),
        "top10_by_vol_pct": _top10_by(lambda s: s.vol_pct),
        "top10_by_liquidity": _top10_by(lambda s: s.liq_points),
    }
    diag["score_distribution"] = score_dist

    # ── 3) Correlation Snapshot (top 10 playbook symbols) ────────────
    top10_syms = [s for s, _ in sorted_items[:10]]
    corr_matrix: Dict[str, Dict[str, float]] = {}
    high_corr_pairs: List[str] = []

    for a in top10_syms:
        row: Dict[str, float] = {}
        for b in top10_syms:
            if a == b:
                row[b] = 1.0
            elif a in close_lists and b in close_lists:
                r = _pearson(close_lists[a], close_lists[b])
                row[b] = r
                if r > 0.85 and a < b:  # avoid duplicates
                    high_corr_pairs.append(f"{a}-{b}({r:.2f})")
            else:
                row[b] = 0.0
        corr_matrix[a] = row

    corr_snapshot = {
        "symbols": top10_syms,
        "matrix": corr_matrix,
        "high_corr_count": len(high_corr_pairs),
        "high_corr_pairs": high_corr_pairs[:10],
        "warning": len(high_corr_pairs) >= 3,
    }
    diag["correlation_snapshot"] = corr_snapshot

    # ── 4) Draft Quality Metrics ─────────────────────────────────────
    total_drafts = len(board_copy)
    from_liquid = sum(1 for s in board_copy if s in _LIQUID_SYMBOLS)
    confidences = [sc.total / _OFF_HOURS_MAX_SCORE for sc in board_copy.values()]
    avg_conf = round(sum(confidences) / len(confidences), 3) if confidences else 0.0
    stop_dists = []
    for sc in board_copy.values():
        if sc.entry_mid > 0 and sc.invalidation > 0:
            stop_dists.append(abs(sc.entry_mid - sc.invalidation) / sc.entry_mid * 100)
    avg_stop_dist = round(sum(stop_dists) / len(stop_dists), 3) if stop_dists else 0.0

    draft_quality = {
        "total_drafts": total_drafts,
        "pct_from_liquid_symbols": round(from_liquid / total_drafts * 100, 1) if total_drafts else 0.0,
        "average_confidence": avg_conf,
        "average_stop_distance_pct": avg_stop_dist,
    }
    diag["draft_quality"] = draft_quality

    # ── 5) News Influence Check ──────────────────────────────────────
    by_news = sorted(board_copy.items(), key=lambda x: x[1].news_count_2h, reverse=True)
    news_influence = []
    for sym, sc in by_news[:5]:
        news_influence.append({
            "symbol": sym,
            "news_count_2h": sc.news_count_2h,
            "total_score": round(sc.total, 1),
            "confidence": round(sc.total / _OFF_HOURS_MAX_SCORE, 3),
            "entry": sc.entry_mid,
            "stop": sc.invalidation,
        })
    diag["news_influence"] = news_influence

    # ── 6) v6 Funnel & Quota Stats ───────────────────────────────────
    funnel_stats = {
        "hard_pool_size": _diag_hard_pool,
        "topN_considered": _OFF_HOURS_FUNNEL_TOP,
        "relaxed_count": _diag_relaxed_count,
        "strict_count": _diag_strict_count,
        "published_count": _diag_published_count,
        "min_news_unmet": _diag_min_news_unmet,
        "quality_dist": dict(_diag_quality_dist),
        "news_points_distribution": dict(_diag_published_news_dist),
        "published_with_news_gte2": sum(
            v for k, v in _diag_published_news_dist.items() if int(k) >= 2
        ),
        "min_news_quota": _PLAYBOOK_MIN_NEWS,
        "v6_skipped_family": _diag_skip_family,
        "v6_skipped_etf": _diag_skip_etf,
        "v6_skipped_corr": _diag_skip_corr,
    }
    diag["v6_funnel"] = funnel_stats

    # ── Print ────────────────────────────────────────────────────────
    lines: List[str] = []
    lines.append("")
    lines.append("=== OFF_HOURS DIAGNOSTIC SNAPSHOT ===")
    lines.append(f"Timestamp: {diag['ts']}")
    lines.append("")

    # 1) Universe
    lines.append("[1] Universe Overview")
    for k, v in overview.items():
        if k == "top10_by_news_count_2h":
            continue  # printed below
        lines.append(f"    {k}: {v}")
    if top10_news_syms:
        lines.append("    top10 symbols by news_count_2h:")
        for sym, cnt in top10_news_syms:
            lines.append(f"      {sym:<7} n2h={cnt}")
    lines.append("")

    # 2) Score distribution
    lines.append(f"[2] Score Distribution (weighted histogram 0-{int(_OFF_HOURS_MAX_SCORE)})")
    bar = "  ".join(f"{k}:{v}" for k, v in histogram.items())
    lines.append(f"    {bar}")
    lines.append("  Top 10 by NEWS:")
    for e in score_dist["top10_by_news"][:5]:
        lines.append(f"    {e['symbol']:<7} news_pts={e['value']}")
    lines.append("  Top 10 by MOMENTUM:")
    for e in score_dist["top10_by_momentum"][:5]:
        lines.append(f"    {e['symbol']:<7} mom_pts={e['value']}")
    lines.append("  Top 10 by VOLATILITY (points):")
    for e in score_dist["top10_by_volatility"][:5]:
        lines.append(f"    {e['symbol']:<7} vol_pts={e['value']}")
    lines.append("  Top 10 by VOL_PCT (raw %):")
    for e in score_dist["top10_by_vol_pct"][:5]:
        lines.append(f"    {e['symbol']:<7} vol_pct={e['value']:.3f}%")
    lines.append("  Top 10 by LIQUIDITY:")
    for e in score_dist["top10_by_liquidity"][:5]:
        lines.append(f"    {e['symbol']:<7} liq_pts={e['value']}")
    lines.append("")

    # 3) Correlation
    lines.append("[3] Correlation Snapshot (top 10 playbook symbols)")
    if top10_syms:
        hdr = "         " + " ".join(f"{s:>6}" for s in top10_syms)
        lines.append(hdr)
        for a in top10_syms:
            row_vals = " ".join(f"{corr_matrix[a].get(b, 0):>6.2f}" for b in top10_syms)
            lines.append(f"  {a:<6}  {row_vals}")
        if high_corr_pairs:
            lines.append(f"  *** HIGH CORR (>{0.85}): {', '.join(high_corr_pairs[:8])}")
        if corr_snapshot["warning"]:
            lines.append("  ⚠ WARNING: >3 pairs with correlation >0.85")
    else:
        lines.append("    (no scored symbols yet)")
    lines.append("")

    # 4) Draft quality
    lines.append("[4] Draft Quality Metrics")
    for k, v in draft_quality.items():
        lines.append(f"    {k}: {v}")
    lines.append("")

    # 5) News influence
    lines.append("[5] News Influence Check (top 5 by news_count_2h)")
    lines.append(f"    {'symbol':<8} {'n2h':>4} {'score':>6} {'conf':>6} {'entry':>9} {'stop':>9}")
    lines.append(f"    {'-'*50}")
    for e in news_influence:
        lines.append(
            f"    {e['symbol']:<8} {e['news_count_2h']:>4} {e['total_score']:>6.1f} "
            f"{e['confidence']:>6.3f} {e['entry']:>9.2f} {e['stop']:>9.2f}"
        )
    lines.append("")

    # 6) v6 Funnel stats
    lines.append("[6] v6 Funnel & Quota Stats")
    lines.append(f"    hard_pool_size: {funnel_stats['hard_pool_size']}")
    lines.append(f"    topN_considered: {funnel_stats['topN_considered']}")
    lines.append(f"    relaxed_count: {funnel_stats['relaxed_count']}")
    lines.append(f"    strict_count: {funnel_stats['strict_count']}")
    lines.append(f"    published_count: {funnel_stats['published_count']}")
    lines.append(f"    min_news_unmet: {funnel_stats['min_news_unmet']}")
    lines.append(f"    quality_dist: {funnel_stats['quality_dist']}")
    lines.append(f"    published_with_news>=2: {funnel_stats['published_with_news_gte2']}")
    lines.append(f"    news_points_dist: {funnel_stats['news_points_distribution']}")
    lines.append(f"    min_news_quota: {funnel_stats['min_news_quota']}")
    lines.append(f"    v6_skipped_family: {funnel_stats['v6_skipped_family']}")
    lines.append(f"    v6_skipped_etf: {funnel_stats['v6_skipped_etf']}")
    lines.append(f"    v6_skipped_corr: {funnel_stats['v6_skipped_corr']}")
    lines.append("")
    lines.append("=== END DIAGNOSTIC ===")

    log.info("%s", "\n".join(lines))

    # ── Write JSON ───────────────────────────────────────────────────
    try:
        _DIAG_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_DIAG_SNAPSHOT_PATH, "w") as f:
            json.dump(diag, f, indent=2, default=str)
        log.debug("Diagnostic snapshot written to %s", _DIAG_SNAPSHOT_PATH)
    except Exception:
        log.exception("Failed to write diagnostic snapshot")


# ── Event handlers ──────────────────────────────────────────────────

def _on_snapshot(snap: MarketSnapshot) -> None:
    """Ingest a market snapshot and evaluate the strategy."""
    global _snapshots_received, _diag_last_cycle_count, _diag_cycle_reset_ts
    now = time.time()

    # Track unique symbols per poll cycle (≈ _POLL_INTERVAL_S window)
    if now - _diag_cycle_reset_ts > _POLL_INTERVAL_S:
        _diag_last_cycle_count = len(_diag_cycle_syms)
        _diag_cycle_syms.clear()
        _diag_cycle_reset_ts = now
    _diag_cycle_syms.add(snap.symbol)

    with _lock:
        _snapshots_received += 1
        sc = _get_cache(snap.symbol)
        sc.snapshots.append(snap)

    _evaluate(snap, sc)


def _on_news(news: NewsEvent) -> None:
    """Ingest a news event into the rolling cache."""
    global _news_received
    now = time.time()

    # ── News Shock Engine: compute impact + burst if enabled ─────────
    if _NEWS_SHOCK_ENABLED:
        age_s = now - (news.ts.timestamp() if hasattr(news.ts, "timestamp") else now)
        n_related = 1  # each NewsEvent is single-symbol; ingest may fan-out
        imp, tags = _compute_news_impact(
            news.headline, n_related, max(0, age_s),
            impact_tags=getattr(news, "impact_tags", None) or [],
        )
        news.impact_score = imp
        news.impact_tags = tags
        news.burst_flag = _update_burst_flag(news.symbol, now)
        if _NEWS_SHOCK_LOG:
            log.info(
                "news_shock  symbol=%s  impact=%d  tags=%s  burst=%s  hl=%s",
                news.symbol, imp, tags, news.burst_flag, news.headline[:60],
            )

    with _lock:
        _news_received += 1
        sc = _get_cache(news.symbol)
        sc.news.append(news)

    log.debug(
        "News cached  symbol=%s  source=%s  sentiment=%s",
        news.symbol,
        news.source,
        news.sentiment,
    )


# ── Strategy evaluation ─────────────────────────────────────────────

def _evaluate(snap: MarketSnapshot, sc: SymbolCache) -> None:
    """Dispatch to the appropriate strategy based on config."""
    now = time.time()

    # ── OFF_HOURS: always score every symbol with enough data ───────
    session = get_us_equity_session()
    if session == OFF_HOURS and len(sc.snapshots) >= 1:
        # Build a lightweight synthetic intent for scoring purposes
        mid = round((snap.bid + snap.ask) / 2.0, 2) if snap.bid > 0 and snap.ask > 0 else snap.last
        inv = round(snap.last * 0.995, 2) if snap.last > 0 else 0.0
        reasons: List[str] = []
        if snap.bid > 0 and snap.ask > 0:
            reasons.append(f"spread={(snap.ask - snap.bid) / snap.last:.4f}")
        synth_intent = TradeIntent(
            symbol=snap.symbol,
            setup_type="OFF_HOURS_SCORE",
            direction="LONG",
            confidence=0.25,
            entry_zone_low=round(snap.bid if snap.bid > 0 else snap.last * 0.999, 2),
            entry_zone_high=round(snap.ask if snap.ask > 0 else snap.last * 1.001, 2),
            invalidation=inv,
            reason_codes=reasons,
        )
        _update_off_hours_score(synth_intent, sc, snap, now)

    # ── PREMARKET: score every symbol that has enough snapshots ──────
    if session == PREMARKET and len(sc.snapshots) >= _PREMARKET_MIN_SNAPSHOTS:
        _load_premarket_playbook()
        _update_premarket_score(snap, sc, now)

    # ── Guard: cooldown ─────────────────────────────────────────────
    if (now - sc.last_intent_ts) < _COOLDOWN_S:
        return

    # ── Guard: hourly cap ───────────────────────────────────────────
    if not _hourly_cap_ok(snap.symbol, now):
        return

    # ── Strategy A: DevMomentum (PAPER/local dev) ───────────────────
    if _DEV_STRATEGY:
        _evaluate_dev_momentum(snap, sc, now)

    # Re-check cooldown (DevMomentum may have just fired)
    if (time.time() - sc.last_intent_ts) < _COOLDOWN_S:
        return
    if not _hourly_cap_ok(snap.symbol, time.time()):
        return

    # ── Strategy B: RSI mean-reversion (always available) ───────────
    _evaluate_rsi(snap, sc, now)


def _evaluate_dev_momentum(snap: MarketSnapshot, sc: SymbolCache, now: float) -> None:
    """DevMomentum: 3-bar rising closes + tight spread → LONG intent.

    Does NOT require RSI.  Only active when ``SIGNAL_DEV_STRATEGY=true``.
    """
    # Need at least 3 snapshots
    if len(sc.snapshots) < 3:
        return

    c1 = sc.snapshots[-3].last  # oldest of the 3
    c2 = sc.snapshots[-2].last
    c3 = sc.snapshots[-1].last  # most recent

    if not (c3 > c2 > c1 > 0):
        return

    # Spread check
    if snap.bid <= 0 or snap.ask <= 0:
        return
    spread_pct = (snap.ask - snap.bid) / snap.last
    if spread_pct > _SPREAD_MAX_PCT:
        return

    reason_codes = [
        f"momentum_3bar={c1:.2f}<{c2:.2f}<{c3:.2f}",
        f"spread={spread_pct:.4f}",
    ]

    intent = TradeIntent(
        symbol=snap.symbol,
        setup_type="DEV_MOMENTUM",
        direction="LONG",
        confidence=0.25,
        entry_zone_low=round(snap.bid, 2),
        entry_zone_high=round(snap.ask, 2),
        invalidation=round(snap.last * 0.995, 2),
        reason_codes=reason_codes,
    )

    _emit_intent(intent, sc, snap, now)


def _evaluate_rsi(snap: MarketSnapshot, sc: SymbolCache, now: float) -> None:
    """RSI mean-reversion: RSI<threshold + price>VWAP + tight spread → LONG."""
    symbol = snap.symbol

    # ── Guard: need RSI to be present ───────────────────────────────
    if snap.rsi14 is None:
        return

    # ── Condition 1: RSI oversold ───────────────────────────────────
    if snap.rsi14 >= _RSI_THRESHOLD:
        return

    # ── Condition 2: price above VWAP (mean-reversion bounce) ──────
    if snap.vwap <= 0 or snap.last <= snap.vwap:
        return

    # ── Condition 3: spread within tolerance ────────────────────────
    if snap.bid <= 0 or snap.ask <= 0:
        return
    spread_pct = (snap.ask - snap.bid) / snap.last
    if spread_pct > _SPREAD_MAX_PCT:
        return

    # ── All conditions met — build confidence ───────────────────────
    confidence = _CONFIDENCE_BASE

    # Boost for positive recent news sentiment
    sentiment = _recent_news_sentiment(sc)
    if sentiment is not None and sentiment > 0:
        confidence = min(1.0, confidence + _CONFIDENCE_NEWS_BOOST * sentiment)

    # ── Build entry zone & invalidation from ATR ────────────────────
    atr = snap.atr if snap.atr > 0 else abs(snap.last - snap.vwap)
    entry_mid = snap.last
    entry_zone_low = round(entry_mid - atr * 0.25, 2)
    entry_zone_high = round(entry_mid + atr * 0.25, 2)
    invalidation = round(entry_mid - atr * _ATR_STOP_MULT, 2)

    reason_codes = [
        f"rsi14={snap.rsi14:.1f}",
        f"above_vwap={snap.last:.2f}>{snap.vwap:.2f}",
        f"spread={spread_pct:.4f}",
    ]
    if sentiment is not None:
        reason_codes.append(f"news_sentiment={sentiment:.2f}")

    intent = TradeIntent(
        symbol=symbol,
        setup_type="mean_revert_rsi",
        direction="LONG",
        confidence=round(confidence, 3),
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        invalidation=invalidation,
        reason_codes=reason_codes,
    )

    _emit_intent(intent, sc, snap, now)


# ── Forced dev intent (pipeline E2E testing) ────────────────────────

def _maybe_force_intent(cache_snapshot: Dict[str, Optional[MarketSnapshot]]) -> None:
    """Emit a synthetic TradeIntent for the first cached symbol.

    Only called when ``SIGNAL_FORCE_INTENT=true``.  Respects a 60 s
    cooldown so the downstream pipeline isn't flooded.
    """
    global _last_forced_intent_ts, _intents_emitted

    now = time.time()
    if (now - _last_forced_intent_ts) < _FORCE_INTENT_INTERVAL_S:
        return

    # Pick the first symbol that has at least one snapshot with a price
    for sym in sorted(cache_snapshot):
        snap = cache_snapshot[sym]
        if snap is None or snap.last <= 0:
            continue

        last = snap.last
        intent = TradeIntent(
            symbol=sym,
            setup_type="FORCED_DEV_TEST",
            direction="LONG",
            confidence=0.2,
            entry_zone_low=round(last * 0.999, 2),
            entry_zone_high=round(last * 1.001, 2),
            invalidation=round(last * 0.99, 2),
            reason_codes=["forced_dev_test"],
        )

        log.warning(
            "FORCED dev intent  symbol=%s  last=%.2f  entry=[%.2f,%.2f]",
            sym, last, intent.entry_zone_low, intent.entry_zone_high,
        )

        _bus.publish(TRADE_INTENT, intent)

        with _lock:
            _last_forced_intent_ts = now
            _intents_emitted += 1
        return  # only one per cycle


# ── Bus connection ──────────────────────────────────────────────────

def _connect_bus():
    """Non-blocking attempt to connect to the event bus and subscribe."""
    try:
        from src.bus.bus_factory import get_bus

        bus = get_bus(max_retries=1)
        if not bus.is_connected:
            log.warning("Event bus unavailable — will retry next cycle")
            return None
        bus.subscribe(MARKET_SNAPSHOT, _on_snapshot, msg_type=MarketSnapshot)
        bus.subscribe(NEWS_EVENT, _on_news, msg_type=NewsEvent)
        log.info("Subscribed to %s, %s", MARKET_SNAPSHOT, NEWS_EVENT)
        return bus
    except Exception:
        log.exception("Failed to initialise event bus — will retry")
        return None


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    """Entry-point for the signal arm."""
    global _bus

    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    log.info(
        "Signal arm starting  mode=%s  rsi_thresh=%.0f  spread_max=%.4f  "
        "cooldown=%ss  dev_strategy=%s  max_intents_per_hour=%d",
        settings.trade_mode.value,
        _RSI_THRESHOLD,
        _SPREAD_MAX_PCT,
        _COOLDOWN_S,
        _DEV_STRATEGY,
        _MAX_INTENTS_PER_SYMBOL_PER_HOUR,
    )

    _bus = _connect_bus()

    last_off_hours_publish_ts: float = 0.0
    last_premarket_publish_ts: float = 0.0

    tick = 0
    while _running:
        tick += 1

        # Lazy reconnect
        if _bus is None:
            _bus = _connect_bus()

        # Own heartbeat
        if _bus is not None:
            _bus.publish(HEARTBEAT, Heartbeat(arm="signal"))

        with _lock:
            syms = len(_cache)
            emitted = _intents_emitted
            snaps_rx = _snapshots_received
            news_rx = _news_received
            cache_snapshot = {
                sym: sc.snapshots[-1] if sc.snapshots else None
                for sym, sc in _cache.items()
            }

        # ── Detailed heartbeat log ───────────────────────────────────
        log.info(
            "heartbeat  tick=%d  symbols=%d  intents_emitted=%d  "
            "snapshots_rx=%d  news_rx=%d",
            tick, syms, emitted, snaps_rx, news_rx,
        )
        for sym, snap in sorted(cache_snapshot.items()):
            if snap is not None:
                rsi_str = f"{snap.rsi14:.1f}" if snap.rsi14 is not None else "n/a"
                log.info(
                    "  %s  last=%.2f  bid=%.2f  ask=%.2f  vwap=%.2f  rsi14=%s",
                    sym, snap.last, snap.bid, snap.ask, snap.vwap, rsi_str,
                )

        # ── OFF_HOURS board publish (every 30 s) ────────────────────
        now_loop = time.time()
        if now_loop - last_off_hours_publish_ts >= _OFF_HOURS_PUBLISH_INTERVAL_S:
            last_off_hours_publish_ts = now_loop
            _publish_off_hours_board()
            if _DIAGNOSTIC_MODE:
                _print_diagnostic()

        # ── PREMARKET board publish (every 30 s) ─────────────────────
        if now_loop - last_premarket_publish_ts >= _PREMARKET_PUBLISH_INTERVAL_S:
            last_premarket_publish_ts = now_loop
            _publish_premarket_board()

        # ── Forced dev intent (pipeline testing only) ────────────────
        if _FORCE_INTENT and _bus is not None:
            _maybe_force_intent(cache_snapshot)

        time.sleep(settings.heartbeat_interval_s)

    # Cleanup
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
    log.info("Signal arm stopped.")


if __name__ == "__main__":
    main()
