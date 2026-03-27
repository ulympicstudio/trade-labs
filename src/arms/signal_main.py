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
from src.signals.regime import (
    update_index as _regime_update_index,
    get_regime as _get_regime,
    STRATEGY_GATE as _STRATEGY_GATE,
    TREND_UP, TREND_DOWN, CHOP, PANIC,
    VOL_HIGH as _VOL_HIGH,
)
from src.signals.event_score import compute_event_score as _compute_event_score
from src.signals.squeeze import (
    update as _squeeze_update,
    get_squeeze as _get_squeeze,
)
from src.signals.sector_intel import (
    update_sector_from_snapshot as _sector_snap,
    update_sector_from_news as _sector_news,
    get_sector_alignment as _get_sector_alignment,
    check_state_changes as _sector_check_states,
)
from src.universe.sector_mapper import classify_symbol as _classify_symbol
from src.signals.volatility_leaders import (
    update_volatility as _vol_update,
    compute_leader as _vol_compute_leader,
    get_leader_summary as _vol_leader_summary,
    VOL_ENABLED as _VOL_ENABLED,
)
from src.signals.industry_rotation import (
    update_industry_rotation as _rotation_snap,
    update_industry_news as _rotation_news,
    update_industry_volatility as _rotation_vol,
    compute_industry_rotation as _rotation_compute,
    get_rotation_summary as _rotation_summary,
    get_rotation_state_bonus as _rotation_bonus,
    ROTATION_ENABLED as _ROTATION_ENABLED,
)
from src.signals.allocation_engine import (
    compute_allocation_decision as _alloc_decide,
    score_symbol_confluence as _alloc_confluence,
    check_bucket_capacity as _alloc_bucket_cap,
    record_bucket_fill as _alloc_record_fill,
    get_allocation_summary as _alloc_summary,
    ALLOC_ENABLED as _ALLOC_ENABLED,
)
from src.signals.market_mode import (
    compute_market_mode as _mm_compute,
    get_market_mode_summary as _mm_summary,
    MODE_ENABLED as _MM_ENABLED,
)
from src.universe.composite_score import (
    compute_composite as _compute_composite,
    COMPOSITE_ENABLED as _COMPOSITE_ENABLED,
)
from src.signals.sector_rotation_selector import (
    compute_sector_rotation_decision as _compute_rotation_decision,
    get_last_rotation_decision as _get_last_rotation_decision,
    ROTATION_SEL_ENABLED as _ROTATION_SEL_ENABLED,
)
from src.universe.dynamic_universe import (
    build_dynamic_universe as _build_dynamic_universe,
    get_last_decision as _get_dyn_universe_decision,
    DYNAMIC_UNIVERSE_ENABLED as _DYN_UNIVERSE_ENABLED,
)
from src.universe.scan_scheduler import (
    build_scan_schedule as _build_scan_schedule,
    get_last_schedule as _get_scan_schedule,
    get_schedule_counts as _get_schedule_counts,
    SCAN_SCHEDULER_ENABLED as _SCAN_SCHEDULER_ENABLED,
    HIGH as _SCAN_HIGH,
)
from src.signals.sector_intel import get_sector_score as _get_sector_score
from src.analysis.playbook_scorecard import is_loss_streak_blocked as _sc_streak_blocked
from src.signals.reentry_harvester import (
    get_reentry_boost as _reentry_get_boost,
    REENTRY_ENABLED as _REENTRY_ENABLED,
)
from src.signals.industry_rotation import get_industry_score as _get_industry_score
from src.analysis.playbook_scorecard import (
    get_priority_bias as _sc_priority_bias,
    get_scorecard_summary as _sc_summary,
    get_loss_streak as _sc_get_loss_streak,
    _sc_get_card_internal,
    SCORECARD_ENABLED as _SC_ENABLED,
)
from src.analysis.self_tuning import (
    get_bucket_weight_nudge as _tune_weight,
    get_priority_nudge as _tune_priority,
    get_threshold_nudge as _tune_threshold,
    get_cap_mult_nudge as _tune_cap_mult,
    compute_tuning_decision as _tune_compute,
    get_tuning_snapshot as _tune_snapshot,
    TUNING_ENABLED as _TUNING_ENABLED,
)
from src.analysis.pnl_attribution import (
    compute_attribution_summary as _attrib_summary,
    ATTRIB_ENABLED as _ATTRIB_ENABLED,
)
from src.signals.sector_intel import get_sector_summary as _get_sector_summary
from src.signals.volatility_leaders import get_top_leaders as _get_vol_top_leaders
from src.signals.industry_rotation import get_top_industries as _get_rotation_top
from src.signals.agent_intel import (
    get_agent_score_boost as _agent_boost,
    get_symbol_intel as _agent_intel,
    get_all_active_intel as _agent_all_active,
)

log = get_logger("signal")

# ── Tunables (all overridable via env) ───────────────────────────────
_RSI_THRESHOLD = float(os.environ.get("TL_SIG_RSI_THRESHOLD", "35"))
_SPREAD_MAX_PCT = float(os.environ.get("TL_SIG_SPREAD_MAX_PCT", "0.003"))  # 0.3 %
_CONFIDENCE_BASE = float(os.environ.get("TL_SIG_CONFIDENCE_BASE", "0.70"))
_CONFIDENCE_NEWS_BOOST = float(os.environ.get("TL_SIG_NEWS_BOOST", "0.10"))
_ATR_STOP_MULT = float(os.environ.get("TL_SIG_ATR_STOP_MULT", "2.0"))
# Per-strategy ATR stop multipliers (override global)
_ATR_MULT_MOMENTUM  = float(os.environ.get("TL_ATR_MULT_MOMENTUM",  "1.8"))  # tight — momentum fades fast
_ATR_MULT_RSI       = float(os.environ.get("TL_ATR_MULT_RSI",       "2.0"))  # standard mean-revert
_ATR_MULT_CONSENSUS = float(os.environ.get("TL_ATR_MULT_CONSENSUS",  "2.2"))  # wider — news moves volatile
_ATR_MULT_VOL       = float(os.environ.get("TL_ATR_MULT_VOL",        "2.5"))  # widest — breakout needs room
_COOLDOWN_S = float(os.environ.get("TL_SIG_COOLDOWN_S", "60"))  # min seconds between intents per symbol
_COOLDOWN_CONSENSUS_S = float(os.environ.get("TL_SIG_COOLDOWN_CONSENSUS_S", "300"))  # longer cooldown after consensus-driven intent
_CONFIDENCE_CONSENSUS_BOOST = float(os.environ.get("TL_SIG_CONSENSUS_CONF_BOOST", "0.15"))  # extra confidence for consensus events
_CONSENSUS_TRADE_ENABLED = os.environ.get("TL_SIG_CONSENSUS_TRADE", "true").lower() in ("1", "true", "yes")
_CONSENSUS_MIN_PROVIDERS = int(os.environ.get("TL_SIG_CONSENSUS_MIN_PROVIDERS", "2"))
_CONSENSUS_MIN_IMPACT = int(os.environ.get("TL_SIG_CONSENSUS_MIN_IMPACT", "3"))
_CACHE_MAX_SNAPS = int(os.environ.get("TL_SIG_CACHE_MAX_SNAPS", "50"))
_CACHE_MAX_NEWS = int(os.environ.get("TL_SIG_CACHE_MAX_NEWS", "20"))
_NEWS_RECENCY_S = float(os.environ.get("TL_SIG_NEWS_RECENCY_S", "1800"))  # 30 min — keeps catalyst alive across full scan cycle
_FORCE_INTENT = os.environ.get("SIGNAL_FORCE_INTENT", "false").lower() in ("1", "true", "yes")
_FORCE_INTENT_INTERVAL_S = float(os.environ.get("TL_TEST_FORCE_INTENT_INTERVAL", "60"))  # emit a forced intent every N seconds

# Dev-strategy flag: auto-enabled in PAPER + local bus, or explicit override
_DEV_STRATEGY_DEFAULT = (
    settings.is_paper
    and os.environ.get("BUS_BACKEND", "local").lower() == "local"
)
_DEV_STRATEGY = os.environ.get(
    "SIGNAL_DEV_STRATEGY", str(_DEV_STRATEGY_DEFAULT)
).lower() in ("1", "true", "yes")

_MAX_INTENTS_PER_SYMBOL_PER_HOUR = 3

# ── EventScore gating thresholds ─────────────────────────────────────
_ES_MIN_RSI = int(os.environ.get("TL_SIG_MIN_EVENT_SCORE_RSI", "22"))
_ES_MIN_DEV = int(os.environ.get("TL_SIG_MIN_EVENT_SCORE_DEV", "18"))
_ES_MIN_CONSENSUS = int(os.environ.get("TL_SIG_MIN_EVENT_SCORE_CONSENSUS", "30"))
_ES_MIN_VOL = int(os.environ.get("TL_SIG_MIN_EVENT_SCORE_VOL", "30"))
_ES_SOFT_MARGIN = 10  # within this margin of threshold → soft penalty

# ── B: Consensus RSI bypass ────────────────────────────────────────────
_CONSENSUS_BYPASS_RSI = os.environ.get(
    "TL_SIG_CONSENSUS_BYPASS_RSI", "true"
).lower() in ("1", "true", "yes")
_CONSENSUS_BYPASS_MIN_PROVIDERS = int(os.environ.get("TL_SIG_CONSENSUS_BYPASS_MIN_PROVIDERS", "3"))

# ── D: Regime strategy gating ───────────────────────────────────────────
_REGIME_GATE_ENABLED = os.environ.get(
    "TL_SIG_REGIME_GATE", "true"
).lower() in ("1", "true", "yes")

# ── F: Adaptive spread filter using ATR% ───────────────────────────
_SPREAD_ATR_MULT = float(os.environ.get("TL_SIG_SPREAD_ATR_MULT", "0.40"))
_SPREAD_MIN = float(os.environ.get("TL_SIG_SPREAD_MIN", "0.0005"))
_SPREAD_MAX = float(os.environ.get("TL_SIG_SPREAD_MAX", "0.0050"))

# ── I: Session awareness ───────────────────────────────────────────────
_SESSION_AWARE = os.environ.get(
    "TL_SIG_SESSION_AWARE", "true"
).lower() in ("1", "true", "yes")
_MIDDAY_MIN_EVENT = int(os.environ.get("TL_SIG_MIDDAY_MIN_EVENT", "60"))
_OPEN_SPREAD_WIDEN = float(os.environ.get("TL_SIG_OPEN_SPREAD_WIDEN", "1.2"))
_last_session_log_ts: float = 0.0

# ── Deterministic test-mode toggles (force-path validation) ──────────
_TEST_FORCE_EVENT_SCORE = int(os.environ.get("TL_TEST_FORCE_EVENT_SCORE", "0"))
_TEST_FORCE_CONSENSUS = int(os.environ.get("TL_TEST_FORCE_CONSENSUS", "0"))
_TEST_FORCE_SPREAD_PCT = float(os.environ.get("TL_TEST_FORCE_SPREAD_PCT", "0"))

# ── Armed-not-triggered observability counters (reset per minute) ────
_obs_event_gate_armed: int = 0
_obs_event_gate_fired: int = 0
_obs_consensus_bypass_armed: int = 0
_obs_consensus_bypass_fired: int = 0
_obs_spread_gate_armed: int = 0
_obs_spread_gate_fired: int = 0
_obs_regime_gate_armed: int = 0
_obs_regime_gate_fired: int = 0
_obs_last_reset_ts: float = 0.0

# ── Blocked strategy reason counters (reset per minute) ──────────────
_blocked_counts: Dict[str, int] = {}       # strategy_name → blocked count
_blocked_reasons: Dict[str, int] = {}      # reason_string → count

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
    last_intent_consensus: bool = False  # whether last intent was consensus-driven

    # EventScore state (populated every _evaluate cycle)
    last_event_score: int = 0
    last_event_playbook: str = ""
    last_event_risk_mode: str = "LOW"

    # Volatility leadership state
    last_vol_score: int = 0
    last_vol_state: str = "QUIET"

    # Industry rotation state
    last_rotation_score: int = 0
    last_rotation_state: str = "NEUTRAL"

    # Allocation / confluence state
    last_priority: float = 0.0
    last_confluence: float = 0.0
    last_bucket: str = "none"

    # Composite intelligence layer
    last_composite_score: float = 0.0
    last_sector_score: float = 0.0
    last_industry_score: float = 0.0
    last_market_score: float = 0.0

    # RSI persistence — last valid RTH rsi14 for fallback when bars are insufficient
    last_known_rsi: Optional[float] = None
    last_known_rsi_date: Optional[str] = None   # ET date str YYYY-MM-DD

    # Phase B: scan priority
    scan_priority: str = "NORMAL"


_cache: Dict[str, SymbolCache] = {}
_lock = threading.Lock()

# ── Runtime state ────────────────────────────────────────────────────
_running = True
_stop_event = threading.Event()  # cooperative shutdown
_bus = None  # type: Any
_intents_emitted = 0
_snapshots_received = 0
_news_received = 0
_last_forced_intent_ts: float = 0.0
_intent_hourly_ts: Dict[str, list] = {}  # symbol → [epoch, …] of recent intents

# ── Phase B: Dynamic Universe + Rotation Selector state ──────────────
_last_rotation_decision_ts: float = 0.0
_ROTATION_DECISION_INTERVAL_S = float(os.environ.get("TL_ROTATION_DECISION_INTERVAL_S", "30"))
_last_scan_schedule: Dict[str, str] = {}  # symbol → HIGH/NORMAL/LOW
_SCAN_PRIORITY_BONUS = float(os.environ.get("TL_SCAN_PRIORITY_BONUS", "3.0"))  # bonus for HIGH priority symbols

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
    # Agent Intelligence boost/penalty
    agent_boost: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.news_points * _W_NEWS
            + self.liq_points * _W_LIQ
            + self.vol_points * _W_VOL
            + self.momentum_pts * _W_MOM
            + self.rsi_points * _W_RSI
            + self.agent_boost
            + self._spread_penalty()
        )

    def _spread_penalty(self) -> float:
        if self.spread_pct < 0.0015:
            return 0.0
        if self.spread_pct < 0.0025:
            return -2.0
        return -4.0


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
    # Agent Intelligence boost/penalty
    agent_boost: float = 0.0

    @property
    def total(self) -> float:
        return (
            self.gap_points * _PREMARKET_GAP_WEIGHT
            + self.vol_points * _PREMARKET_VOL_WEIGHT
            + self.news_points * _PREMARKET_NEWS_WEIGHT
            + self.rsi_points * _PREMARKET_RSI_WEIGHT
            + self.rvol_points  # rvol_points already weighted (0-3)
            + self.agent_boost
            + self._spread_penalty()
        )

    def _spread_penalty(self) -> float:
        if self.spread_pct < 0.0015:
            return 0.0
        if self.spread_pct < 0.0025:
            return -2.0
        return -4.0


_premarket_board: Dict[str, _PremarketScore] = {}
_premarket_board_lock = threading.Lock()
_premarket_playbook: Dict[str, Dict] = {}    # symbol → playbook draft dict
_premarket_playbook_loaded: bool = False


def _handle_signal(signum, _frame):
    global _running
    log.info("Received shutdown signal (%s)", signum)
    _running = False
    _stop_event.set()


# ── Cache helpers ────────────────────────────────────────────────────

def _scorecard_cap_adj(bucket: str, cap_mult: float) -> float:
    """Reduce cap_mult when a bucket is on a losing streak."""
    card = _sc_get_card_internal(bucket)
    streak = _sc_get_loss_streak(bucket)
    if card is None or streak == 0:
        return cap_mult
    scale = 1.0 if streak < 1 else (0.75 if streak < 3 else (0.50 if streak < 5 else 0.25))
    adjusted = round(cap_mult * scale, 3)
    return adjusted


def _fetch_historical_bars(symbol: str, bar_size: str = "1 min", lookback: int = 20) -> list:
    """
    Fetch historical bars from IBKR for RSI pre-seeding.
    Returns list of close prices (float), newest last.
    Returns [] if TWS is unavailable — caller handles gracefully.
    """
    try:
        from src.broker.ib_session import get_ib
        from ib_insync import Stock
        ib   = get_ib()
        contract = Stock(symbol, "SMART", "USD")
        # duration: 1 day covers 390 min bars (full RTH session)
        duration = f"{lookback + 5} S" if "sec" in bar_size else "2 D"
        bars = ib.reqHistoricalData(
            contract,
            endDateTime="",
            durationStr=duration,
            barSizeSetting=bar_size,
            whatToShow="TRADES",
            useRTH=True,
            formatDate=1,
            keepUpToDate=False,
        )
        closes = [b.close for b in bars if b.close > 0]
        log.debug("_fetch_historical_bars sym=%s bars=%d", symbol, len(closes))
        return closes[-lookback:] if len(closes) > lookback else closes
    except Exception as exc:
        log.debug("_fetch_historical_bars unavailable sym=%s err=%s", symbol, exc)
        return []


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
            continue  # skip stale; deque may be out-of-order
        if n.sentiment is not None:
            scores.append(n.sentiment)
    return (sum(scores) / len(scores)) if scores else None


def _recent_consensus_count(sc: SymbolCache) -> int:
    """Return max consensus provider count from recent news (last ``_NEWS_RECENCY_S``).

    Scans impact_tags for ``CONSENSUS:N`` tags.  Returns 0 if no consensus.
    """
    now = time.time()
    best = 0
    for n in reversed(sc.news):
        age = now - n.ts.timestamp() if hasattr(n.ts, "timestamp") else _NEWS_RECENCY_S + 1
        if age > _NEWS_RECENCY_S:
            continue  # skip stale; deque may be out-of-order
        for tag in (n.impact_tags or []):
            if tag.startswith("CONSENSUS:"):
                try:
                    best = max(best, int(tag.split(":")[1]))
                except (IndexError, ValueError):
                    pass
    return best


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
        sc.last_intent_consensus = any(
            rc.startswith("consensus=") for rc in intent.reason_codes
        )
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

    # ── Agent Intelligence boost ─────────────────────────────────
    _ab = _agent_boost(sym)
    if _ab != 0.0:
        reasons.append(f"agent={_ab:+.1f}")

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
        agent_boost=_ab,
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

    # Sort: composite (if enabled) → total desc → news_count desc → recency desc
    def _off_hours_sort_key(x):
        sym, sc = x
        if _COMPOSITE_ENABLED:
            _c = _cache.get(sym)
            _cscore = _c.last_composite_score if _c else 0.0
            return (-_cscore, -sc.total, -sc.news_count_2h, -sc.last_update_ts)
        return (-sc.total, -sc.news_count_2h, -sc.last_update_ts)
    hard_pool.sort(key=_off_hours_sort_key)
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
    _regime_now = _get_regime()
    for sym, sc, quality in top_n:
        conf = round(sc.total / _OFF_HOURS_MAX_SCORE, 3)
        # Append quality + low_quality marker to reason codes
        pub_reasons = list(sc.reason_codes[:6])
        pub_reasons.append(f"quality={quality}")
        if quality == "LOW":
            pub_reasons.append("low_quality")

        # ── Agent Intelligence enrichment ──────────────────────
        _sym_ai = _agent_intel(sym)
        if _sym_ai is not None:
            pub_reasons.append(f"agent_catalyst={_sym_ai.catalyst_type}")
            if _sym_ai.risk_flags:
                pub_reasons.append(f"agent_risk={','.join(_sym_ai.risk_flags)}")

        # ── Event gate fields for structural gating ──────────────────
        _sym_cache = _cache.get(sym)
        _sym_es = _sym_cache.last_event_score if _sym_cache else 0
        _gate_pass = _sym_es >= _ES_MIN_DEV
        _tradeable = _gate_pass and quality in ("HIGH", "MED")
        _sp = _classify_symbol(sym)

        # ── Composite intelligence fields ────────────────────────
        _sc_cache = _cache.get(sym)
        _comp_sc = _sc_cache.last_composite_score if _sc_cache else 0.0
        _sec_sc = _sc_cache.last_sector_score if _sc_cache else 0.0
        _ind_sc = _sc_cache.last_industry_score if _sc_cache else 0.0
        _mkt_sc = _sc_cache.last_market_score if _sc_cache else 0.0

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
            symbol_score=float(sc.total),
            sector_score=_sec_sc,
            industry_score=_ind_sc,
            market_score=_mkt_sc,
            composite_score=_comp_sc,
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
            event_score=_sym_es,
            strategy="off_hours_board",
            event_gate_pass=_gate_pass,
            tradeable=_tradeable,
            regime=_regime_now.regime,
            sector=_sp.sector,
            industry=_sp.industry,
            sector_state=_get_sector_alignment(sym).sector_state,
            symbol_score=float(sc.total),
            sector_score=_sec_sc,
            industry_score=_ind_sc,
            market_score=_mkt_sc,
            composite_score=_comp_sc,
        )
        log.info(
            "open_plan_candidate_created symbol=%s score=%d strategy=%s gate_pass=%s sector=%s",
            sym, _sym_es, "off_hours_board", _gate_pass, _sp.sector,
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

    # ── Agent Intelligence boost ─────────────────────────────────
    _ab = _agent_boost(sym)
    if _ab != 0.0:
        reasons.append(f"agent={_ab:+.1f}")

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
        agent_boost=_ab,
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

    # Sort: composite (if enabled) → total desc → gap_pct abs desc → news desc
    def _premarket_sort_key(x):
        sym, sc = x
        if _COMPOSITE_ENABLED:
            _c = _cache.get(sym)
            _cscore = _c.last_composite_score if _c else 0.0
            return (-_cscore, -sc.total, -abs(sc.gap_pct), -sc.news_count_2h)
        return (-sc.total, -abs(sc.gap_pct), -sc.news_count_2h)
    filtered.sort(key=_premarket_sort_key)

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
    _regime_now = _get_regime()
    for sym, sc in top_n:
        conf = round(sc.total / _PREMARKET_MAX_SCORE, 3) if _PREMARKET_MAX_SCORE > 0 else 0.0

        pub_reasons = list(sc.reason_codes[:6])
        pub_reasons.append(f"quality={sc.quality}")
        if sc.playbook_entry > 0:
            pub_reasons.append(f"pb_entry={sc.playbook_entry:.2f}")
        if sc.rvol > 0:
            pub_reasons.append(f"rvol={sc.rvol:.1f}x")

        # ── Agent Intelligence enrichment ──────────────────────
        _sym_ai = _agent_intel(sym)
        if _sym_ai is not None:
            pub_reasons.append(f"agent_catalyst={_sym_ai.catalyst_type}")
            if _sym_ai.risk_flags:
                pub_reasons.append(f"agent_risk={','.join(_sym_ai.risk_flags)}")

        # ── Event gate fields for structural gating ──────────────────
        _sym_cache = _cache.get(sym)
        _sym_es = _sym_cache.last_event_score if _sym_cache else 0
        _gate_pass = _sym_es >= _ES_MIN_DEV
        _tradeable = _gate_pass and sc.quality in ("HIGH", "MED")
        _sp = _classify_symbol(sym)

        # ── Composite intelligence fields ────────────────────────
        _comp_sc = _sym_cache.last_composite_score if _sym_cache else 0.0
        _sec_sc = _sym_cache.last_sector_score if _sym_cache else 0.0
        _ind_sc = _sym_cache.last_industry_score if _sym_cache else 0.0
        _mkt_sc = _sym_cache.last_market_score if _sym_cache else 0.0

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
            event_score=_sym_es,
            strategy="premarket_board",
            event_gate_pass=_gate_pass,
            tradeable=_tradeable,
            regime=_regime_now.regime,
            sector=_sp.sector,
            industry=_sp.industry,
            sector_state=_get_sector_alignment(sym).sector_state,
            symbol_score=float(sc.total),
            sector_score=_sec_sc,
            industry_score=_ind_sc,
            market_score=_mkt_sc,
            composite_score=_comp_sc,
        )
        log.info(
            "open_plan_candidate_created symbol=%s score=%d strategy=%s gate_pass=%s sector=%s",
            sym, _sym_es, "premarket_board", _gate_pass, _sp.sector,
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
            symbol_score=float(sc.total),
            sector_score=_sec_sc,
            industry_score=_ind_sc,
            market_score=_mkt_sc,
            composite_score=_comp_sc,
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

_first_snapshot_logged = False
_first_news_logged = False


def _on_snapshot(snap: MarketSnapshot) -> None:
    """Ingest a market snapshot and evaluate the strategy."""
    global _snapshots_received, _diag_last_cycle_count, _diag_cycle_reset_ts
    global _first_snapshot_logged
    now = time.time()

    # ── RSI pre-seed on first snapshot for this symbol ────────────
    if snap.rsi14 is None:
        _sc_tmp = _get_cache(snap.symbol)
        if _sc_tmp.last_known_rsi is None:
            bars = _fetch_historical_bars(snap.symbol, bar_size="1 min", lookback=20)
            if bars:
                from src.signals.indicators import compute_rsi as _compute_rsi
                _pre_rsi = _compute_rsi(bars, period=14)
                if _pre_rsi is not None:
                    snap.rsi14 = _pre_rsi
                    _sc_tmp.last_known_rsi = _pre_rsi
                    log.debug("rsi_pre_seed sym=%s rsi14=%.1f from %d bars", snap.symbol, _pre_rsi, len(bars))

    # Log first receipt once per run
    if not _first_snapshot_logged:
        _first_snapshot_logged = True
        log.info(
            "signal first snapshot received  sym=%s  last=%.2f  bid=%.2f  "
            "ask=%.2f  rsi14=%s  session=%s",
            snap.symbol, snap.last, snap.bid, snap.ask,
            snap.rsi14, snap.session,
        )

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

    # Feed regime detector (index symbols: SPY, QQQ)
    _regime_update_index(
        snap.symbol, snap.last,
        max(snap.last, snap.ask) if snap.ask > 0 else snap.last,
        min(snap.last, snap.bid) if snap.bid > 0 else snap.last,
    )
    # Feed squeeze detector
    _squeeze_update(
        snap.symbol, snap.last,
        max(snap.last, snap.ask) if snap.ask > 0 else snap.last,
        min(snap.last, snap.bid) if snap.bid > 0 else snap.last,
        snap.volume,
    )
    # Feed sector intelligence tracker
    _sector_snap(snap.symbol, snap.last)
    # Check for sector state changes + periodic heartbeat
    _sector_check_states()

    # Feed volatility leadership tracker
    _vol_update(
        snap.symbol, snap.last, snap.bid, snap.ask,
        snap.volume, snap.atr, getattr(snap, 'rvol', None),
    )

    # Feed industry rotation tracker
    _rotation_snap(snap.symbol, snap.last)

    _evaluate(snap, sc)


def _on_news(news: NewsEvent) -> None:
    """Ingest a news event into the rolling cache."""
    global _news_received, _first_news_logged
    now = time.time()

    # Log first receipt once per run
    if not _first_news_logged:
        _first_news_logged = True
        log.info(
            "signal first news received  sym=%s  impact=%d  tags=%s  hl=%s",
            news.symbol, news.impact_score,
            getattr(news, 'impact_tags', []),
            news.headline[:80],
        )

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

    # Feed sector intelligence tracker
    _sector_news(news.symbol, getattr(news, "impact_score", 0))

    # Feed industry rotation tracker
    _rotation_news(news.symbol, getattr(news, "impact_score", 0))

    # Always log consensus-tagged news (regardless of NEWS_SHOCK_ENABLED)
    _tags = getattr(news, "impact_tags", None) or []
    _has_consensus = any(t.startswith("CONSENSUS:") for t in _tags)
    if _has_consensus:
        log.info(
            "news_consensus_rx  symbol=%s  impact=%s  tags=%s  provider=%s  hl=%s",
            news.symbol,
            getattr(news, "impact_score", "?"),
            _tags,
            getattr(news, "source_provider", "?"),
            news.headline[:80],
        )
    # ── News-triggered re-evaluation ────────────────────────────────
    # For high-impact news, immediately re-score the symbol using the
    # last known snapshot rather than waiting for the next scan cycle.
    _news_impact = getattr(news, "impact_score", 0) or 0
    if _news_impact >= 3 or _has_consensus:
        with _lock:
            _sc = _get_cache(news.symbol)
            _last_snap = _sc.snapshots[-1] if _sc.snapshots else None
        if _last_snap is not None:
            try:
                _evaluate(_last_snap, _sc)
                log.debug(
                    "news_triggered_eval symbol=%s impact=%d consensus=%s",
                    news.symbol, _news_impact, _has_consensus,
                )
            except Exception as _e:
                log.warning("news_triggered_eval_error symbol=%s err=%s", news.symbol, _e)

    else:
        log.debug(
            "News cached  symbol=%s  source=%s  sentiment=%s",
            news.symbol,
            news.source,
            news.sentiment,
        )


# ── Session-phase classifier (I) ────────────────────────────────────

def _get_session_phase() -> str:
    """Return fine-grained session phase for strategy gating.

    PREMARKET, OPEN (first 30m), MIDDAY (11:00–14:00), POWER_HOUR (last 1h),
    RTH (remaining RTH), AFTERHOURS, OFF_HOURS.
    """
    from datetime import datetime, timezone, timedelta, time as dtime
    now_utc = datetime.now(timezone.utc)
    # Reuse the session module's ET conversion
    from src.market.session import _to_et
    et = _to_et(now_utc)
    t = et.time()
    if t < dtime(4, 0):
        return "OFF_HOURS"
    if t < dtime(9, 30):
        return "PREMARKET"
    if t < dtime(10, 0):
        return "OPEN"
    if t < dtime(11, 0):
        return "RTH"
    if t < dtime(14, 0):
        return "MIDDAY"
    if t < dtime(15, 0):
        return "RTH"
    if t < dtime(16, 0):
        return "POWER_HOUR"
    if t < dtime(20, 0):
        return "AFTERHOURS"
    return "OFF_HOURS"


# ── Adaptive spread filter (F) ──────────────────────────────────────

def _adaptive_spread_limit(snap: MarketSnapshot, phase: str) -> float:
    """Compute dynamic spread limit from ATR%, clamped to [_SPREAD_MIN, _SPREAD_MAX].

    During OPEN phase, limits are widened by ``_OPEN_SPREAD_WIDEN``.
    Falls back to static ``_SPREAD_MAX_PCT`` when ATR is unavailable.
    """
    if snap.atr and snap.atr > 0 and snap.last > 0:
        atr_pct = snap.atr / snap.last
        limit = atr_pct * _SPREAD_ATR_MULT
    else:
        limit = _SPREAD_MAX_PCT
    limit = max(_SPREAD_MIN, min(_SPREAD_MAX, limit))
    if phase == "OPEN":
        limit *= _OPEN_SPREAD_WIDEN
    return limit


# ── Regime strategy gate (D) ────────────────────────────────────────

def _regime_allows(strat_name: str, regime: str) -> bool:
    """Return True if regime gate allows *strat_name*.

    When ``_REGIME_GATE_ENABLED`` is False, always returns True.
    """
    global _obs_regime_gate_armed, _obs_regime_gate_fired
    _obs_regime_gate_armed += 1
    if not _REGIME_GATE_ENABLED:
        return True
    # Map internal strategy names → gate-set keys
    _NAME_MAP = {
        "DEV_MOMENTUM": "momentum",
        "mean_revert_rsi": "mean_revert_rsi",
        "consensus_news": "consensus_news",
        "volatility_breakout": "breakout",
    }
    gate_name = _NAME_MAP.get(strat_name, strat_name)
    allowed = _STRATEGY_GATE.get(regime, set())
    if gate_name not in allowed:
        _obs_regime_gate_fired += 1
        _blocked_counts[strat_name] = _blocked_counts.get(strat_name, 0) + 1
        _blocked_reasons["regime_gate"] = _blocked_reasons.get("regime_gate", 0) + 1
    return gate_name in allowed


# ── EventScore gate helper ──────────────────────────────────────────

def _check_event_gate(
    symbol: str, strat: str, score: int, min_score: int, reasons: list,
) -> bool:
    """Return True if *strat* is allowed by the EventScore gate.

    Logs a skip line when blocked.  When score is within ``_ES_SOFT_MARGIN``
    of the threshold the strategy is still allowed but confidence will be
    reduced in the intent builder (see ``_apply_event_soft_penalty``).
    """
    global _obs_event_gate_armed, _obs_event_gate_fired
    _obs_event_gate_armed += 1
    if score < min_score:
        _obs_event_gate_fired += 1
        _blocked_counts[strat] = _blocked_counts.get(strat, 0) + 1
        _blocked_reasons["low_event_score"] = _blocked_reasons.get("low_event_score", 0) + 1
        log.info(
            "event_gate_skip symbol=%s strat=%s score=%d min=%d reasons=%s",
            symbol, strat, score, min_score, reasons,
        )
        return False
    return True


def _apply_event_soft_penalty(
    confidence: float, score: int, min_score: int,
) -> tuple[float, bool]:
    """Reduce confidence slightly when score is near gate threshold.

    Returns (adjusted_confidence, was_penalized).
    """
    if min_score <= score < min_score + _ES_SOFT_MARGIN:
        return max(0.05, round(confidence - 0.05, 3)), True
    return confidence, False


# ── Strategy evaluation ─────────────────────────────────────────────

def _evaluate(snap: MarketSnapshot, sc: SymbolCache) -> None:
    """Dispatch to the appropriate strategy based on config."""
    now = time.time()

    # Global declarations for observability counters (used throughout)
    global _obs_event_gate_armed, _obs_event_gate_fired
    global _obs_consensus_bypass_armed, _obs_consensus_bypass_fired
    global _obs_spread_gate_armed, _obs_spread_gate_fired
    global _obs_regime_gate_armed, _obs_regime_gate_fired
    global _obs_last_reset_ts

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
    effective_cooldown = _COOLDOWN_CONSENSUS_S if sc.last_intent_consensus else _COOLDOWN_S
    if (now - sc.last_intent_ts) < effective_cooldown:
        return

    # ── Guard: hourly cap ───────────────────────────────────────────
    if not _hourly_cap_ok(snap.symbol, now):
        return

    # ── Unified Event Score (observability + future gating) ──────────
    _spread = (snap.ask - snap.bid) / snap.last if snap.bid > 0 and snap.ask > 0 and snap.last > 0 else None
    # Force-path override: spread
    if _TEST_FORCE_SPREAD_PCT > 0:
        _spread = _TEST_FORCE_SPREAD_PCT
    _consensus = _recent_consensus_count(sc)
    # Force-path override: consensus provider count
    if _TEST_FORCE_CONSENSUS > 0:
        _consensus = _TEST_FORCE_CONSENSUS
    _best_impact = 0
    _cat_tags: List[str] = []
    _cutoff = now - _NEWS_RECENCY_S
    for _n in reversed(sc.news):
        _nts = _n.ts.timestamp() if hasattr(_n.ts, "timestamp") else 0
        if _nts < _cutoff:
            continue  # skip stale; don't break — deque may be out-of-order
        _imp = getattr(_n, "impact_score", 0) or 0
        if _imp > _best_impact:
            _best_impact = _imp
        _cat_tags.extend(t for t in (_n.impact_tags or []) if not t.startswith("CONSENSUS:"))

    _regime = _get_regime()
    _sq = _get_squeeze(snap.symbol)
    _sa = _get_sector_alignment(snap.symbol)
    _es = _compute_event_score(
        consensus_n=_consensus,
        impact_score=_best_impact,
        category_tags=_cat_tags or None,
        sentiment=_recent_news_sentiment(sc),
        rsi14=snap.rsi14,
        rvol=getattr(snap, 'rvol', None),
        spread_pct=_spread,
        regime=_regime.regime,
        regime_confidence=_regime.confidence,
        sector_align_pts=_sa.pts_sector_align,
        sector_rs_pts=_sa.pts_sector_rs,
        sector_heat_pts=_sa.pts_sector_heat,
        sector_sympathy_pts=_sa.pts_sector_sympathy,
        sector_name=_sa.sector,
        industry_name=_sa.industry,
        sector_state=_sa.sector_state,
    )
    # Force-path override: event score
    if _TEST_FORCE_EVENT_SCORE > 0:
        from dataclasses import replace as _dc_replace
        _es = _dc_replace(_es, event_score=_TEST_FORCE_EVENT_SCORE,
                          reasons=_es.reasons + [f"test_force_es={_TEST_FORCE_EVENT_SCORE}"])

    # ── Industry rotation bonus/penalty on event score ───────────────
    _rot_preview = _rotation_compute(snap.symbol)
    _rot_bonus = _rotation_bonus(_rot_preview.rotation_state)
    if _rot_bonus != 0:
        from dataclasses import replace as _dc_replace2
        _adj_score = max(0, min(100, _es.event_score + _rot_bonus))
        _es = _dc_replace2(_es, event_score=_adj_score,
                           reasons=_es.reasons + [f"rotation_{_rot_preview.rotation_state}→{_rot_bonus:+d}"])

    # ── Compact per-component breakdown (always log for observability) ──
    log.info(
        "event_score_breakdown sym=%s score=%d consensus=%.0f cat=%.0f "
        "impact=%.0f rsi=%.0f rvol=%.0f spread=%.0f regime=%.0f "
        "sector=%.0f sector_name=%s sector_state=%s "
        "playbook=%s regime_state=%s",
        snap.symbol, _es.event_score,
        _es.pts_consensus, _es.pts_category, _es.pts_impact,
        _es.pts_rsi, _es.pts_rvol, _es.pts_spread, _es.pts_regime,
        _es.pts_sector, _es.sector, _es.sector_state,
        _es.playbook, _regime.regime,
    )

    if _es.event_score >= 30 or _sq.squeeze_score >= 30:
        log.info(
            "event_score symbol=%s es=%d playbook=%s risk=%s regime=%s "
            "squeeze=%d(%s) consensus=%d impact=%d rsi=%s reasons=%s",
            snap.symbol, _es.event_score, _es.playbook, _es.risk_mode,
            _regime.regime, _sq.squeeze_score, _sq.squeeze_state,
            _consensus, _best_impact,
            f"{snap.rsi14:.1f}" if snap.rsi14 is not None else "n/a",
            _es.reasons,
        )

    # Store EventScore on cache for strategy gating
    sc.last_event_score = _es.event_score
    sc.last_event_playbook = _es.playbook
    sc.last_event_risk_mode = _es.risk_mode

    # ── Volatility leadership compute ────────────────────────────────
    _vl = _vol_compute_leader(snap.symbol, _regime.regime, _sa.sector_state)
    sc.last_vol_score = _vl.leader_score
    sc.last_vol_state = _vl.leader_state
    if _vl.leader_score > 0:
        log.info(
            "volatility_leader sym=%s score=%d state=%s rvol=%.1f "
            "atrx=%.1f spread=%.4f range=%.1f compress=%s reasons=%s",
            snap.symbol, _vl.leader_score, _vl.leader_state,
            _vl.rvol_ratio, _vl.atr_expansion_ratio,
            _vl.spread_pct, _vl.range_expansion_pct,
            _vl.compression_releasing, _vl.reasons,
        )
        # Feed volatility leader hit into industry rotation
        _rotation_vol(snap.symbol)

    # ── Industry rotation compute ────────────────────────────────────
    _rot = _rotation_compute(snap.symbol)
    sc.last_rotation_score = _rot.rotation_score
    sc.last_rotation_state = _rot.rotation_state
    if _rot.rotation_score > 0:
        log.info(
            "industry_rotation sym=%s sector=%s industry=%s state=%s "
            "score=%d rs=%.2f breadth=%.0f heat=%.1f vol_leaders=%d n=%d",
            snap.symbol, _rot.sector, _rot.industry, _rot.rotation_state,
            _rot.rotation_score, _rot.relative_strength, _rot.breadth,
            _rot.news_heat, _rot.vol_leaders, _rot.symbols_tracked,
        )

    # ── Playbook balance (observability) ─────────────────────────────
    _pb_news = 0.35 if _consensus > 0 or _best_impact > 0 else 0.0
    _pb_sector = 0.30 if _sa.sector_state in ("BULLISH", "HOT") else 0.10
    _pb_vol = 0.35 if _vl.leader_state in ("TRIGGERED", "BUILDING") else 0.0
    _pb_rotation = 0.20 if _rot.rotation_state in ("LEADING", "ROTATING_IN") else 0.0
    _pb_total = _pb_news + _pb_sector + _pb_vol + _pb_rotation or 1.0
    log.info(
        "playbook_balance news=%.2f sector=%.2f vol=%.2f rotation=%.2f regime=%s",
        _pb_news / _pb_total, _pb_sector / _pb_total, _pb_vol / _pb_total,
        _pb_rotation / _pb_total, _regime.regime,
    )

    # ── Market Mode / Session Commander ────────────────────────────────
    _session_now = get_us_equity_session()
    _mm_d = None
    if _MM_ENABLED:
        _sec_sum = _get_sector_summary()
        _sec_states = {k: v.get("state", "NEUTRAL") for k, v in _sec_sum.items()}
        _sec_breadths = {k: v.get("breadth", 50.0) for k, v in _sec_sum.items()}
        _rot_tops = _get_rotation_top(8)
        _rot_leaders = sum(1 for r in _rot_tops if r.rotation_state in ("LEADING", "ROTATING_IN"))
        _rot_top_score = max((r.rotation_score for r in _rot_tops), default=0)
        _vol_tops = _get_vol_top_leaders(10)
        _vol_triggered = sum(1 for v in _vol_tops if v.leader_state == "TRIGGERED")
        _vol_top_score_mm = max((v.leader_score for v in _vol_tops), default=0)
        _mm_d = _mm_compute(
            regime=_regime.regime,
            session=_session_now,
            sector_states=_sec_states,
            sector_breadths=_sec_breadths,
            rotation_leaders=_rot_leaders,
            rotation_top_score=_rot_top_score,
            vol_triggered_count=_vol_triggered,
            vol_top_score=_vol_top_score_mm,
            avg_event_score=float(_es.event_score),
            news_hot_count=1 if _es.event_score > 60 else 0,
        )
        log.info(
            "market_mode_decision mode=%s conf=%.2f posture=%s breadth=%s "
            "vol=%s rot=%s news=%s cap_mult=%.2f "
            "weights=[n=%.2f r=%.2f v=%.2f m=%.2f] reasons=%s",
            _mm_d.mode, _mm_d.confidence, _mm_d.risk_posture,
            _mm_d.breadth_state, _mm_d.volatility_state,
            _mm_d.rotation_state, _mm_d.news_state,
            _mm_d.position_cap_mult,
            _mm_d.recommended_news_weight, _mm_d.recommended_rotation_weight,
            _mm_d.recommended_vol_weight, _mm_d.recommended_meanrev_weight,
            _mm_d.reasons,
        )

    # ── Allocation Decision (once per eval cycle) ────────────────────
    _alloc_d = _alloc_decide(regime=_regime.regime, session=_session_now, market_mode=_mm_d)
    if _ALLOC_ENABLED:
        log.info(
            "allocation_decision regime=%s session=%s bias=%s posture=%s "
            "weights=[n=%.2f r=%.2f v=%.2f m=%.2f] maxpos=%d reasons=%s",
            _alloc_d.regime, _alloc_d.session_state, _alloc_d.market_bias,
            _alloc_d.risk_posture,
            _alloc_d.weight_news, _alloc_d.weight_rotation,
            _alloc_d.weight_volatility, _alloc_d.weight_meanrevert,
            _alloc_d.max_total_positions, _alloc_d.reasons,
        )

    # ── Self-Tuning: apply weight nudges to allocation ───────────────
    if _TUNING_ENABLED and _ALLOC_ENABLED:
        _tw_n = _tune_weight("news")
        _tw_r = _tune_weight("rotation")
        _tw_v = _tune_weight("volatility")
        _tw_m = _tune_weight("meanrevert")
        if any(x != 0 for x in (_tw_n, _tw_r, _tw_v, _tw_m)):
            # AllocationDecision is frozen — log adjusted values as observability
            _adj_n = max(0.0, min(1.0, _alloc_d.weight_news + _tw_n))
            _adj_r = max(0.0, min(1.0, _alloc_d.weight_rotation + _tw_r))
            _adj_v = max(0.0, min(1.0, _alloc_d.weight_volatility + _tw_v))
            _adj_m = max(0.0, min(1.0, _alloc_d.weight_meanrevert + _tw_m))
            log.info(
                "tuned_bucket_weight news=%+.4f rot=%+.4f vol=%+.4f mr=%+.4f "
                "adj=[n=%.2f r=%.2f v=%.2f m=%.2f]",
                _tw_n, _tw_r, _tw_v, _tw_m,
                _adj_n, _adj_r, _adj_v, _adj_m,
            )

    # ── Symbol priority / confluence scoring ─────────────────────────
    _conf = _alloc_confluence(
        symbol=snap.symbol,
        event_score=_es.event_score,
        sector_state=_sa.sector_state,
        sector_score=sc.last_sector_score,
        rotation_state=_rot.rotation_state,
        rotation_score=_rot.rotation_score,
        vol_state=_vl.leader_state,
        vol_score=_vl.leader_score,
        regime=_regime.regime,
        spread_pct=_spread if _spread is not None else 0.0,
        session=_session_now,
        decision=_alloc_d,
    )
    sc.last_confluence = _conf.confluence_score
    sc.last_bucket = _conf.bucket

    # ── Scorecard cap_mult feedback ───────────────────────────────────
    cap_mult = _mm_d.position_cap_mult if _mm_d else 1.0
    if _SC_ENABLED and _conf.bucket != "none":
        cap_mult = _scorecard_cap_adj(_conf.bucket, cap_mult)
        log.info("scorecard_cap_adj bucket=%s streak=%d cap_mult=%.3f", _conf.bucket, _sc_get_loss_streak(_conf.bucket), cap_mult)

    if _conf.priority_score > 0:
        log.info(
            "symbol_priority sym=%s priority=%.1f confluence=%.2f "
            "bucket=%s matched=%s reasons=%s",
            snap.symbol, _conf.priority_score, _conf.confluence_score,
            _conf.bucket, _conf.matched_engines, _conf.reasons,
        )
    if len(_conf.matched_engines) >= 2:
        log.info(
            "confluence_hit sym=%s matched=%s bonus=%.2f priority=%.1f",
            snap.symbol, _conf.matched_engines,
            _conf.confluence_score, _conf.priority_score,
        )
    if _mm_d is not None and _conf.priority_score > 0:
        _mode_fit = "HIGH" if _conf.priority_score >= 50 else ("MED" if _conf.priority_score >= 25 else "LOW")
        log.info(
            "symbol_mode_fit sym=%s mode=%s fit=%s priority=%.1f bucket=%s",
            snap.symbol, _mm_d.mode, _mode_fit,
            _conf.priority_score, _conf.bucket,
        )

    # ── Scorecard priority bias ──────────────────────────────────────
    if _SC_ENABLED and _conf.bucket != "none":
        _sc_bias = _sc_priority_bias(_conf.bucket)
        if _sc_bias != 0.0:
            _old_pri = _conf.priority_score
            _conf.priority_score = round(_conf.priority_score + _sc_bias, 2)
            log.info(
                "scorecard_fit sym=%s bucket=%s bias=%+.1f priority=%.1f→%.1f",
                snap.symbol, _conf.bucket, _sc_bias,
                _old_pri, _conf.priority_score,
            )

    # ── Self-Tuning: priority bias nudge ─────────────────────────────
    if _TUNING_ENABLED and _conf.bucket != "none":
        _tp_nudge = _tune_priority(_conf.bucket)
        if _tp_nudge != 0.0:
            _old_pri = _conf.priority_score
            _conf.priority_score = round(max(0.0, _conf.priority_score + _tp_nudge), 2)
            log.info(
                "tuned_priority_bias sym=%s bucket=%s nudge=%+.2f priority=%.1f→%.1f",
                snap.symbol, _conf.bucket, _tp_nudge,
                _old_pri, _conf.priority_score,
            )

    # ── Re-entry harvester boost ──────────────────────────────────────
    if _REENTRY_ENABLED:
        _reentry_boost = _reentry_get_boost(snap.symbol)
        if _reentry_boost > 0.0:
            _old_pri = _conf.priority_score
            _conf.priority_score = round(_conf.priority_score + _reentry_boost, 2)
            log.info(
                "reentry_boost_applied sym=%s boost=%.1f priority=%.1f→%.1f",
                snap.symbol, _reentry_boost, _old_pri, _conf.priority_score,
            )
    sc.last_priority = _conf.priority_score

    # ── Self-Tuning: event threshold nudge ───────────────────────────
    if _TUNING_ENABLED and _conf.bucket != "none":
        _tt_nudge = _tune_threshold(_conf.bucket)
        if _tt_nudge != 0.0:
            log.info(
                "tuned_threshold sym=%s bucket=%s nudge=%+.1f",
                snap.symbol, _conf.bucket, _tt_nudge,
            )

    # ── Composite Intelligence Layer ─────────────────────────────────
    if _COMPOSITE_ENABLED:
        _symbol_sc = float(_es.event_score)
        _comp = _compute_composite(
            symbol=snap.symbol,
            symbol_score=_symbol_sc,
            market_mode_decision=_mm_d,
        )
        sc.last_composite_score = _comp.composite_score
        sc.last_sector_score = _comp.sector_score
        # Persist last valid RSI for fallback when intraday bars insufficient
        if snap.rsi14 is not None:
            sc.last_known_rsi = snap.rsi14
            from src.market.session import _to_et
            from datetime import timezone
            import datetime as _dt
            sc.last_known_rsi_date = _to_et(
                _dt.datetime.now(timezone.utc)
            ).strftime("%Y-%m-%d")
        sc.last_industry_score = _comp.industry_score
        sc.last_market_score = _comp.market_score
        if _comp.composite_score > 30:
            log.info(
                "composite_score sym=%s composite=%.1f symbol=%.1f "
                "sector=%.1f industry=%.1f market=%.1f "
                "sector_name=%s industry_name=%s",
                snap.symbol, _comp.composite_score, _comp.symbol_score,
                _comp.sector_score, _comp.industry_score, _comp.market_score,
                _comp.sector, _comp.industry,
            )

    # ── Phase B: Scan priority bonus ─────────────────────────────────
    if sc.scan_priority == _SCAN_HIGH and _SCAN_PRIORITY_BONUS > 0:
        sc.last_composite_score = sc.last_composite_score + _SCAN_PRIORITY_BONUS

    # ── I: Session phase awareness ──────────────────────────────────
    global _last_session_log_ts
    _phase = _get_session_phase()
    if now - _last_session_log_ts >= 60.0:
        _last_session_log_ts = now
        log.info("session_state=%s", _phase)

        # ── Armed-not-triggered observability summary (per minute) ──
        log.info(
            "obs_gates event_gate=%d/%d consensus_bypass=%d/%d "
            "spread_gate=%d/%d regime_gate=%d/%d",
            _obs_event_gate_fired, _obs_event_gate_armed,
            _obs_consensus_bypass_fired, _obs_consensus_bypass_armed,
            _obs_spread_gate_fired, _obs_spread_gate_armed,
            _obs_regime_gate_fired, _obs_regime_gate_armed,
        )
        _obs_event_gate_armed = _obs_event_gate_fired = 0
        _obs_consensus_bypass_armed = _obs_consensus_bypass_fired = 0
        _obs_spread_gate_armed = _obs_spread_gate_fired = 0
        _obs_regime_gate_armed = _obs_regime_gate_fired = 0
        _obs_last_reset_ts = now

        # ── Blocked strategy summary (per minute) ───────────────────
        if _blocked_counts or _blocked_reasons:
            # Format: DEV=120 RSI=120 CONS=120
            strat_parts = " ".join(f"{k}={v}" for k, v in sorted(_blocked_counts.items()))
            # Top reasons: low_event_score=340, regime_gate=80, high_spread=20
            top_reasons = sorted(_blocked_reasons.items(), key=lambda x: -x[1])[:5]
            reason_str = ", ".join(f"{k}={v}" for k, v in top_reasons)
            log.info(
                "blocked_summary %s top_reasons=[%s]",
                strat_parts, reason_str,
            )
            _blocked_counts.clear()
            _blocked_reasons.clear()

    # ── D: Regime gate helper (per-strategy) ────────────────────────
    # Accumulate scorecard bias + tuning nudge into effective gate reduction
    _bias_total = 0.0
    if _SC_ENABLED and _conf.bucket != "none":
        _bias_total += _sc_priority_bias(_conf.bucket)
    if _TUNING_ENABLED and _conf.bucket != "none":
        _bias_total += _tune_threshold(_conf.bucket)
    # Each +10 bias points reduces the gate minimum by 1 (capped at -5)
    _gate_reduction = int(max(-5.0, min(0.0, -abs(_bias_total) * 0.1 if _bias_total > 0 else 0.0)))

    def _gated(strat: str) -> bool:
        """Check both event-score gate AND regime gate.  Log skip."""
        _base_min = {"DEV_MOMENTUM": _ES_MIN_DEV, "mean_revert_rsi": _ES_MIN_RSI,
                     "consensus_news": _ES_MIN_CONSENSUS,
                     "volatility_breakout": _ES_MIN_VOL}.get(strat, 0)
        # ── Regime-adaptive gate: relax thresholds in low-activity regimes ──
        _regime_adj = 0
        _current_regime = _regime.regime if _regime else "UNKNOWN"
        if strat == "consensus_news":
            if _current_regime in ("CHOP", "RANGE"):
                _regime_adj = -13   # 45 → 32: CHOP has fewer catalysts, relax
            elif _current_regime == "BEAR":
                _regime_adj = -8    # 45 → 37: still cautious in bear
        elif strat == "mean_revert_rsi":
            if _current_regime in ("CHOP", "RANGE"):
                _regime_adj = -8    # 35 → 27: RSI mean-revert is primary CHOP edge
        elif strat == "volatility_breakout":
            if _current_regime in ("TREND", "BREAKOUT"):
                _regime_adj = -5    # 30 → 25: vol breakout thrives in trend
            elif _current_regime == "CHOP":
                _regime_adj = +5    # 30 → 35: tighten vol gate in CHOP
        _effective_min = max(10, _base_min + _gate_reduction + _regime_adj)
        if _gate_reduction != 0:
            log.debug(
                "gate_bias_applied sym=%s strat=%s base_min=%d bias=%.1f effective_min=%d",
                snap.symbol, strat, _base_min, _bias_total, _effective_min,
            )
        if not _check_event_gate(snap.symbol, strat, sc.last_event_score,
                                 _effective_min, _es.reasons):
            return False
        if not _regime_allows(strat, _regime.regime):
            log.debug(
                "regime_gate_skip regime=%s setup=%s symbol=%s",
                _regime.regime, strat, snap.symbol,
            )
            return False
        # Loss streak gate — block bucket after 3 consecutive losses
        _STREAK_MAX = int(os.environ.get("TL_LOSS_STREAK_MAX", "3"))
        if _SC_ENABLED and _conf.bucket != "none" and _sc_streak_blocked(_conf.bucket, _STREAK_MAX):
            log.info(
                "loss_streak_gate_skip sym=%s bucket=%s streak>=%d",
                snap.symbol, _conf.bucket, _STREAK_MAX,
            )
            return False
        return True

    # ── I: Session-based strategy restrictions ──────────────────────
    _allow_momentum = True
    _allow_rsi = True
    _allow_consensus = _CONSENSUS_TRADE_ENABLED
    if _SESSION_AWARE:
        if _phase == "PREMARKET":
            _allow_momentum = False
            _allow_rsi = False  # consensus/news only
        elif _phase == "MIDDAY" and sc.last_event_score < _MIDDAY_MIN_EVENT:
            _allow_momentum = False  # disable momentum unless high-event
        # OPEN: handled by adaptive spread widening in _adaptive_spread_limit

    # ── Strategy A: DevMomentum (PAPER/local dev) ───────────────────
    if _DEV_STRATEGY and _allow_momentum:
        if _gated("DEV_MOMENTUM"):
            _evaluate_dev_momentum(snap, sc, now)

    # Re-check cooldown (DevMomentum may have just fired)
    if (time.time() - sc.last_intent_ts) < _COOLDOWN_S:
        return
    if not _hourly_cap_ok(snap.symbol, time.time()):
        return

    # ── Strategy B: RSI mean-reversion (always available) ───────────
    if _allow_rsi and _gated("mean_revert_rsi"):
        _evaluate_rsi(snap, sc, now)

    # Re-check cooldown (RSI may have just fired)
    if (time.time() - sc.last_intent_ts) < _COOLDOWN_S:
        return
    if not _hourly_cap_ok(snap.symbol, time.time()):
        return

    # ── Strategy C: Consensus news (fires without RSI) ──────────────
    if _allow_consensus:
        _obs_consensus_bypass_armed += 1
        if _gated("consensus_news"):
            _obs_consensus_bypass_fired += 1
            _evaluate_consensus_news(snap, sc, now)

    # Re-check cooldown (Consensus may have just fired)
    if (time.time() - sc.last_intent_ts) < _COOLDOWN_S:
        return
    if not _hourly_cap_ok(snap.symbol, time.time()):
        return

    # ── Strategy D: Volatility breakout (stocks only) ───────────────
    if _VOL_ENABLED and _vl.leader_state == "TRIGGERED":
        if _gated("volatility_breakout"):
            _evaluate_volatility_breakout(snap, sc, now, _vl)


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

    # Spread check (F: adaptive)
    if snap.bid <= 0 or snap.ask <= 0:
        return
    spread_pct = (snap.ask - snap.bid) / snap.last
    _spread_limit = _adaptive_spread_limit(snap, _get_session_phase())
    global _obs_spread_gate_armed, _obs_spread_gate_fired
    _obs_spread_gate_armed += 1
    if spread_pct > _spread_limit:
        _obs_spread_gate_fired += 1
        _blocked_reasons["high_spread"] = _blocked_reasons.get("high_spread", 0) + 1
        log.debug(
            "spread_gate_skip symbol=%s spread=%.4f atr_limit=%.4f",
            snap.symbol, spread_pct, _spread_limit,
        )
        return

    reason_codes = [
        f"momentum_3bar={c1:.2f}<{c2:.2f}<{c3:.2f}",
        f"spread={spread_pct:.4f}",
    ]

    conf = 0.25
    consensus_n = _recent_consensus_count(sc)
    # Force-path override: consensus provider count
    if _TEST_FORCE_CONSENSUS > 0:
        consensus_n = _TEST_FORCE_CONSENSUS
    if consensus_n >= 2:
        conf = min(1.0, conf + _CONFIDENCE_CONSENSUS_BOOST)
        reason_codes.append(f"consensus={consensus_n}")

    if consensus_n >= 2:
        log.info(
            "CONSENSUS_SIGNAL symbol=%s confidence=%.2f providers=%d",
            snap.symbol, conf, consensus_n,
        )

    # EventScore soft penalty
    conf, _penalized = _apply_event_soft_penalty(conf, sc.last_event_score, _ES_MIN_DEV)
    if _penalized:
        reason_codes.append("event_gate_soft")
    reason_codes.append(f"event_score={sc.last_event_score}")

    intent = TradeIntent(
        symbol=snap.symbol,
        setup_type="DEV_MOMENTUM",
        direction="LONG",
        confidence=conf,
        entry_zone_low=round(snap.bid, 2),
        entry_zone_high=round(snap.ask, 2),
        invalidation=round(snap.last - (snap.atr if snap.atr > 0 else snap.last * 0.01) * _ATR_MULT_MOMENTUM, 2),
        reason_codes=reason_codes,
    )

    _emit_intent(intent, sc, snap, now)


def _evaluate_rsi(snap: MarketSnapshot, sc: SymbolCache, now: float) -> None:
    """RSI mean-reversion: RSI<threshold + price>VWAP + tight spread → LONG."""
    symbol = snap.symbol

    # ── Guard: use live RSI or fall back to last known RTH value ───────
    _rsi = snap.rsi14 if snap.rsi14 is not None else sc.last_known_rsi
    if _rsi is None:
        return
    # Staleness check: if RSI is from a prior session, apply confidence haircut
    _rsi_is_stale = False
    if snap.rsi14 is None and sc.last_known_rsi is not None:
        from src.market.session import _to_et
        from datetime import timezone
        import datetime as _dt
        _today_et = _to_et(_dt.datetime.now(timezone.utc)).strftime("%Y-%m-%d")
        _rsi_is_stale = (sc.last_known_rsi_date != _today_et)

    # ── Condition 1: RSI oversold ───────────────────────────────────
    if _rsi >= _RSI_THRESHOLD:
        return

    # ── Condition 2: price above VWAP (mean-reversion bounce) ──────
    if snap.vwap <= 0 or snap.last <= snap.vwap:
        return

    # ── Condition 3: spread within tolerance (F: adaptive) ────────────
    if snap.bid <= 0 or snap.ask <= 0:
        return
    spread_pct = (snap.ask - snap.bid) / snap.last
    _spread_limit = _adaptive_spread_limit(snap, _get_session_phase())
    global _obs_spread_gate_armed, _obs_spread_gate_fired
    _obs_spread_gate_armed += 1
    if spread_pct > _spread_limit:
        _obs_spread_gate_fired += 1
        log.debug(
            "spread_gate_skip symbol=%s spread=%.4f atr_limit=%.4f strat=mean_revert_rsi",
            symbol, spread_pct, _spread_limit,
        )
        return

    # ── All conditions met — build confidence ───────────────────────
    confidence = _CONFIDENCE_BASE
    if _rsi_is_stale:
        confidence *= 0.60   # prior-session RSI: treat as soft bias, not fresh signal

    # Boost for positive recent news sentiment
    sentiment = _recent_news_sentiment(sc)
    if sentiment is not None and sentiment > 0:
        confidence = min(1.0, confidence + _CONFIDENCE_NEWS_BOOST * sentiment)

    # Boost for cross-provider consensus
    consensus_n = _recent_consensus_count(sc)
    if consensus_n >= 2:
        confidence = min(1.0, confidence + _CONFIDENCE_CONSENSUS_BOOST)

    # ── Build entry zone & invalidation from ATR ────────────────────
    atr = snap.atr if snap.atr > 0 else abs(snap.last - snap.vwap)
    entry_mid = snap.last
    entry_zone_low = round(entry_mid - atr * 0.25, 2)
    entry_zone_high = round(entry_mid + atr * 0.25, 2)
    invalidation = round(entry_mid - atr * _ATR_MULT_RSI, 2)

    reason_codes = [
        f"rsi14={_rsi:.1f}",
        f"above_vwap={snap.last:.2f}>{snap.vwap:.2f}",
        f"spread={spread_pct:.4f}",
    ]
    if sentiment is not None:
        reason_codes.append(f"news_sentiment={sentiment:.2f}")
    if consensus_n >= 2:
        reason_codes.append(f"consensus={consensus_n}")

    if consensus_n >= 2:
        log.info(
            "CONSENSUS_SIGNAL symbol=%s confidence=%.2f providers=%d",
            symbol, confidence, consensus_n,
        )

    # EventScore soft penalty
    confidence, _penalized = _apply_event_soft_penalty(confidence, sc.last_event_score, _ES_MIN_RSI)
    if _penalized:
        reason_codes.append("event_gate_soft")
    reason_codes.append(f"event_score={sc.last_event_score}")

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


# ── Strategy C: Consensus-driven news trade ─────────────────────────

def _evaluate_consensus_news(snap: MarketSnapshot, sc: SymbolCache, now: float) -> None:
    """Fire a trade when multi-provider consensus is strong, even without RSI.

    Conditions:
        1. CONSENSUS:N tag with N >= ``_CONSENSUS_MIN_PROVIDERS``
        2. ``impact_score`` >= ``_CONSENSUS_MIN_IMPACT``
        3. Valid spread
    This allows the system to react to cross-source news events immediately
    on startup (before 14 candles of RSI history accumulate).
    """
    symbol = snap.symbol

    consensus_n = _recent_consensus_count(sc)
    # Force-path override: consensus provider count
    if _TEST_FORCE_CONSENSUS > 0:
        consensus_n = _TEST_FORCE_CONSENSUS
    if consensus_n < _CONSENSUS_MIN_PROVIDERS:
        _blocked_reasons["no_consensus"] = _blocked_reasons.get("no_consensus", 0) + 1
        log.debug(
            "consensus_skip symbol=%s reason=insufficient_providers providers=%d min=%d",
            symbol, consensus_n, _CONSENSUS_MIN_PROVIDERS,
        )
        return

    # Best impact score from recent consensus news
    best_impact = 0
    cutoff = now - _NEWS_RECENCY_S
    for n in reversed(sc.news):
        ts = n.ts.timestamp() if hasattr(n.ts, "timestamp") else 0
        if ts < cutoff:
            break
        imp = getattr(n, "impact_score", 0) or 0
        if imp > best_impact:
            best_impact = imp
    if best_impact < _CONSENSUS_MIN_IMPACT:
        log.debug(
            "consensus_skip symbol=%s reason=low_impact best_impact=%d min=%d",
            symbol, best_impact, _CONSENSUS_MIN_IMPACT,
        )
        return

    # Spread guard (F: adaptive)
    if snap.bid <= 0 or snap.ask <= 0:
        log.debug("consensus_skip symbol=%s reason=no_bid_ask", symbol)
        return
    spread_pct = (snap.ask - snap.bid) / snap.last
    _spread_limit = _adaptive_spread_limit(snap, _get_session_phase())
    global _obs_spread_gate_armed, _obs_spread_gate_fired
    _obs_spread_gate_armed += 1
    if spread_pct > _spread_limit:
        _obs_spread_gate_fired += 1
        log.debug(
            "spread_gate_skip symbol=%s spread=%.4f atr_limit=%.4f strat=consensus_news",
            symbol, spread_pct, _spread_limit,
        )
        return

    # Confidence: conservative base + consensus boost (cap at 0.85)
    confidence = min(0.85, 0.50 + _CONFIDENCE_CONSENSUS_BOOST + 0.05 * (consensus_n - 2))

    # Entry zone: tight around mid
    mid = round((snap.bid + snap.ask) / 2.0, 2)
    half = round(max(0.01, (snap.ask - snap.bid) * 0.5), 2)
    entry_zone_low = round(mid - half, 2)
    entry_zone_high = round(mid + half, 2)
    _con_atr = snap.atr if snap.atr > 0 else snap.last * 0.012
    invalidation = round(snap.last - _con_atr * _ATR_MULT_CONSENSUS, 2)

    reason_codes = [
        "consensus_only",
        f"consensus={consensus_n}",
        f"impact={best_impact}",
        f"spread={spread_pct:.4f}",
    ]

    log.info(
        "CONSENSUS_SIGNAL symbol=%s confidence=%.2f providers=%d impact=%d",
        symbol, confidence, consensus_n, best_impact,
    )

    # EventScore soft penalty
    confidence, _penalized = _apply_event_soft_penalty(confidence, sc.last_event_score, _ES_MIN_CONSENSUS)
    if _penalized:
        reason_codes.append("event_gate_soft")
    reason_codes.append(f"event_score={sc.last_event_score}")

    intent = TradeIntent(
        symbol=symbol,
        setup_type="consensus_news",
        direction="LONG",
        confidence=round(confidence, 3),
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        invalidation=invalidation,
        reason_codes=reason_codes,
    )

    _emit_intent(intent, sc, snap, now)


# ── Strategy D: Volatility breakout ─────────────────────────────────

def _evaluate_volatility_breakout(
    snap: MarketSnapshot, sc: SymbolCache, now: float, vl,
) -> None:
    """Volatility breakout: TRIGGERED leader with good spread → LONG intent.

    Stocks only.  Requires leader_state == TRIGGERED (checked by caller).
    """
    symbol = snap.symbol

    # Spread guard
    if snap.bid <= 0 or snap.ask <= 0:
        return
    spread_pct = (snap.ask - snap.bid) / snap.last
    _spread_limit = _adaptive_spread_limit(snap, _get_session_phase())
    if spread_pct > _spread_limit:
        log.debug(
            "volatility_gate_skip symbol=%s reason=high_spread spread=%.4f limit=%.4f",
            symbol, spread_pct, _spread_limit,
        )
        return

    # Confidence: base + volatility boost (cap at 0.85)
    confidence = min(0.85, _CONFIDENCE_BASE + vl.confidence_boost)

    # Entry zone: tight around mid
    mid = round((snap.bid + snap.ask) / 2.0, 2)
    half = round(max(0.01, (snap.ask - snap.bid) * 0.5), 2)
    entry_zone_low = round(mid - half, 2)
    entry_zone_high = round(mid + half, 2)
    invalidation = round(snap.last * (1.0 - 0.01 * max(1.0, vl.atr_expansion_ratio)), 2)

    reason_codes = [
        "volatility_breakout",
        f"vol_score={vl.leader_score}",
        f"vol_state={vl.leader_state}",
        f"rvol={vl.rvol_ratio:.1f}",
        f"atrx={vl.atr_expansion_ratio:.1f}",
        f"spread={spread_pct:.4f}",
    ]
    if vl.compression_releasing:
        reason_codes.append("squeeze_release")

    log.info(
        "VOLATILITY_SIGNAL symbol=%s confidence=%.2f score=%d state=%s "
        "rvol=%.1f atrx=%.1f",
        symbol, confidence, vl.leader_score, vl.leader_state,
        vl.rvol_ratio, vl.atr_expansion_ratio,
    )

    # EventScore soft penalty
    confidence, _penalized = _apply_event_soft_penalty(confidence, sc.last_event_score, _ES_MIN_VOL)
    if _penalized:
        reason_codes.append("event_gate_soft")
    reason_codes.append(f"event_score={sc.last_event_score}")

    intent = TradeIntent(
        symbol=symbol,
        setup_type="volatility_breakout",
        direction="LONG",
        confidence=round(confidence, 3),
        entry_zone_low=entry_zone_low,
        entry_zone_high=entry_zone_high,
        invalidation=invalidation,
        reason_codes=reason_codes,
    )

    _emit_intent(intent, sc, snap, now)


# ── Forced dev intent (pipeline E2E testing) ────────────────────────

# Force-sector symbol selection
_TEST_FORCE_SECTOR = os.environ.get("TL_TEST_FORCE_SECTOR", "")

def _maybe_force_intent(cache_snapshot: Dict[str, Optional[MarketSnapshot]]) -> None:
    """Emit a synthetic TradeIntent for the first cached symbol.

    Only called when ``SIGNAL_FORCE_INTENT=true``.  Respects a 60 s
    cooldown so the downstream pipeline isn't flooded.

    When ``TL_TEST_FORCE_SECTOR`` is set, strongly prefers a symbol from
    that sector so the forced intent exercises the sector pipeline.
    """
    global _last_forced_intent_ts, _intents_emitted

    now = time.time()
    if (now - _last_forced_intent_ts) < _FORCE_INTENT_INTERVAL_S:
        return

    # ── Symbol selection: prefer forced-sector match ─────────────────
    chosen_sym: Optional[str] = None
    fallback = False

    if _TEST_FORCE_SECTOR:
        from src.universe.sector_mapper import get_sector_symbols
        sector_syms = set(get_sector_symbols(_TEST_FORCE_SECTOR))
        # Pick the first sector-matching symbol that has an active snapshot
        for sym in sorted(cache_snapshot):
            if sym not in sector_syms:
                continue
            snap = cache_snapshot[sym]
            if snap is not None and snap.last > 0:
                chosen_sym = sym
                break

    if chosen_sym is None:
        fallback = _TEST_FORCE_SECTOR != ""
        # Fall back to first symbol with a valid price
        for sym in sorted(cache_snapshot):
            snap = cache_snapshot[sym]
            if snap is not None and snap.last > 0:
                chosen_sym = sym
                break

    if chosen_sym is None:
        return

    _chosen_sp = _classify_symbol(chosen_sym)
    log.info(
        "forced_intent_symbol symbol=%s sector=%s reason=%s fallback=%s",
        chosen_sym, _chosen_sp.sector,
        "force_sector_match" if not fallback else "no_sector_match",
        str(fallback).lower(),
    )

    snap = cache_snapshot[chosen_sym]
    last = snap.last
    _forced_es = _TEST_FORCE_EVENT_SCORE or 55
    intent = TradeIntent(
        symbol=chosen_sym,
        setup_type="FORCED_DEV_TEST",
        direction="LONG",
        confidence=0.2,
        entry_zone_low=round(last * 0.999, 2),
        entry_zone_high=round(last * 1.001, 2),
        invalidation=round(last * 0.99, 2),
        reason_codes=["forced_dev_test", f"event_score={_forced_es}"],
    )

    log.warning(
        "FORCED dev intent  symbol=%s  last=%.2f  entry=[%.2f,%.2f]",
        chosen_sym, last, intent.entry_zone_low, intent.entry_zone_high,
    )

    _bus.publish(TRADE_INTENT, intent)

    with _lock:
        _last_forced_intent_ts = now
        _intents_emitted += 1


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


# ── Top-10 candidate board (printed every 60 s) ─────────────────────

_CANDIDATE_BOARD_TOP_N = 10
_CANDIDATE_BOARD_INTERVAL_S = 60.0


def _print_candidate_board() -> None:
    """Log a ranked table of the top candidates for quick tuning visibility.

    For each symbol: event_score, consensus, spread, regime, squeeze, gate status.
    """
    regime = _get_regime()
    entries = []

    with _lock:
        for sym, sc in _cache.items():
            if not sc.snapshots:
                continue
            snap = sc.snapshots[-1]
            # Spread
            spread_pct = 0.0
            if snap.bid > 0 and snap.ask > 0 and snap.last > 0:
                spread_pct = (snap.ask - snap.bid) / snap.last

            # Consensus
            cons = _recent_consensus_count(sc)

            # Squeeze
            sq = _get_squeeze(sym)

            # Gate status
            gates = []
            es = sc.last_event_score
            if es < _ES_MIN_DEV:
                gates.append("event_gate")
            if not regime.allows_strategy("momentum"):
                gates.append("regime_gate")
            if spread_pct > _SPREAD_MAX:
                gates.append("spread_gate")
            gate_str = ",".join(gates) if gates else "CLEAR"

            entries.append((
                sym, es, cons,
                spread_pct, regime.regime,
                sq.squeeze_score, gate_str,
                snap.last,
            ))

    # Sort by event_score desc
    entries.sort(key=lambda x: -x[1])
    top = entries[:_CANDIDATE_BOARD_TOP_N]

    if not top:
        return

    log.info("candidate_board:")
    for i, (sym, es, cons, spread, reg, sqz, gate, price) in enumerate(top, 1):
        log.info(
            "  %2d %-5s score=%d cons=%d spread=%.2f%% regime=%s squeeze=%d "
            "blocked=%s last=%.2f",
            i, sym, es, cons,
            spread * 100, reg, sqz, gate, price,
        )


# ── Main loop ────────────────────────────────────────────────────────

def main() -> None:
    """Entry-point for the signal arm."""
    global _bus, _last_rotation_decision_ts

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

    # ── Force-path calibration banner ─────────────────────────────
    _force_regime = os.environ.get("TL_TEST_FORCE_REGIME", "")
    # ── Volatility engine banner ──────────────────────────────────
    from src.signals.volatility_leaders import (
        _FORCE_SCORE as _vf_score, _FORCE_STATE as _vf_state,
        _FORCE_SYMBOL as _vf_sym,
    )
    if _VOL_ENABLED:
        log.info(
            "VOLATILITY_ENGINE enabled  min_score=%d  force_score=%d  "
            "force_state=%s  force_symbol=%s",
            _ES_MIN_VOL, _vf_score, _vf_state or "(natural)",
            _vf_sym or "(all)",
        )

    # ── Industry rotation engine banner ───────────────────────────
    if _ROTATION_ENABLED:
        from src.signals.industry_rotation import (
            _FORCE_INDUSTRY as _rf_ind,
            _FORCE_ROTATION_STATE as _rf_state,
            _FORCE_ROTATION_SCORE as _rf_score,
        )
        log.info(
            "ROTATION_ENGINE enabled  force_industry=%s  "
            "force_state=%s  force_score=%d",
            _rf_ind or "(natural)",
            _rf_state or "(natural)",
            _rf_score,
        )

    # ── Allocation engine banner ──────────────────────────────────
    if _ALLOC_ENABLED:
        from src.signals.allocation_engine import (
            _FORCE_MODE as _af_mode,
            _F_NEWS as _af_news, _F_ROTATION as _af_rot,
            _F_VOL as _af_vol, _F_MEANREV as _af_mr,
            _F_MAX_POS as _af_maxpos,
        )
        log.info(
            "ALLOCATION_ENGINE enabled  force_mode=%s  "
            "force_news=%.2f  force_rot=%.2f  "
            "force_vol=%.2f  force_mr=%.2f  force_maxpos=%d",
            _af_mode or "(natural)",
            _af_news, _af_rot, _af_vol, _af_mr, _af_maxpos,
        )

    # ── Market Mode / Session Commander banner ────────────────────
    if _MM_ENABLED:
        from src.signals.market_mode import (
            _FORCE_MODE as _mf_mode,
            _FORCE_CONFIDENCE as _mf_conf,
            _FORCE_NEWS as _mf_news, _FORCE_ROTATION as _mf_rot,
            _FORCE_VOL as _mf_vol, _FORCE_MEANREV as _mf_mr,
            _FORCE_CAP_MULT as _mf_cap,
        )
        log.info(
            "MARKET_MODE_ENGINE enabled  force_mode=%s  "
            "force_conf=%.2f  force_news=%.2f  force_rot=%.2f  "
            "force_vol=%.2f  force_mr=%.2f  force_cap=%.2f",
            _mf_mode or "(natural)", _mf_conf,
            _mf_news, _mf_rot, _mf_vol, _mf_mr, _mf_cap,
        )

    if _SC_ENABLED:
        from src.analysis.playbook_scorecard import (
            _FORCE_PLAYBOOK as _scf_pb,
            _FORCE_SCORE as _scf_score,
        )
        log.info(
            "SCORECARD_ENGINE enabled  force_playbook=%s  force_score=%.2f",
            _scf_pb or "(natural)", _scf_score,
        )

    # ── Exit Intelligence Engine banner ───────────────────────────
    try:
        from src.risk.exit_intelligence import (
            EXIT_ENABLED as _exit_on,
            _FORCE_PLAYBOOK as _ef_pb,
            _FORCE_MODE as _ef_mode,
            _FORCE_ACTION as _ef_action,
        )
        if _exit_on:
            log.info(
                "EXIT_ENGINE enabled  force_playbook=%s  force_mode=%s  force_action=%s",
                _ef_pb or "(natural)", _ef_mode or "(natural)",
                _ef_action or "(natural)",
            )
    except Exception:
        pass

    # ── PnL Attribution + Self-Tuning Engine banner ───────────────
    if _ATTRIB_ENABLED:
        log.info("ATTRIBUTION_ENGINE enabled")
    if _TUNING_ENABLED:
        from src.analysis.self_tuning import (
            _FORCE_BUCKET as _tun_fb,
            _FORCE_EDGE as _tun_fe,
            _FORCE_SAMPLE as _tun_fs,
            _FORCE_WEIGHT_DELTA as _tun_fw,
            _FORCE_THRESHOLD_DELTA as _tun_ft,
        )
        log.info(
            "TUNING_ENGINE enabled  force_bucket=%s  force_edge=%s  "
            "force_sample=%d  force_weight_delta=%.3f  force_threshold_delta=%.1f",
            _tun_fb or "(natural)", _tun_fe or "(natural)",
            _tun_fs, _tun_fw, _tun_ft,
        )

    if _force_regime or _TEST_FORCE_EVENT_SCORE or _TEST_FORCE_CONSENSUS or _TEST_FORCE_SPREAD_PCT:
        log.warning(
            "FORCE-PATH MODE ACTIVE  regime=%s  event_score=%d  "
            "consensus=%d  spread_pct=%.4f",
            _force_regime or "(natural)",
            _TEST_FORCE_EVENT_SCORE,
            _TEST_FORCE_CONSENSUS,
            _TEST_FORCE_SPREAD_PCT,
        )

    _bus = _connect_bus()

    last_off_hours_publish_ts: float = 0.0
    last_premarket_publish_ts: float = 0.0
    last_candidate_board_ts: float = 0.0

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
        _regime = _get_regime()
        _rot_hb = ""
        if _ROTATION_ENABLED:
            _rot_hb = f"  {_rotation_summary()}"
        _alloc_hb = ""
        if _ALLOC_ENABLED:
            _alloc_hb = f"  {_alloc_summary()}"
        _mm_hb = ""
        if _MM_ENABLED:
            _mm_hb = f"  {_mm_summary()}"
        _sc_hb = ""
        if _SC_ENABLED:
            _sc_snap = _sc_summary()
            _sc_hb = f"  SC[closed={_sc_snap.get('total_closed', 0)} open={_sc_snap.get('open_trades', 0)} conf={_sc_snap.get('overall_confidence', 1.0):.2f}]"
        _tune_hb = ""
        if _TUNING_ENABLED:
            _ts = _tune_snapshot()
            _tune_hb = f"  TUNE[ovr={_ts.active_overrides}]"
            if _ts.active_overrides > 0:
                log.info(
                    "tuning_override_applied active=%d bucket=%s priority=%s threshold=%s cap=%s qty=%s",
                    _ts.active_overrides,
                    {k: round(v, 4) for k, v in _ts.bucket_nudges.items() if v != 0},
                    {k: round(v, 2) for k, v in _ts.priority_nudges.items() if v != 0},
                    {k: round(v, 1) for k, v in _ts.threshold_nudges.items() if v != 0},
                    {k: round(v, 4) for k, v in _ts.cap_mult_nudges.items() if v != 0},
                    {k: round(v, 4) for k, v in _ts.qty_mult_nudges.items() if v != 0},
                )
        log.info(
            "heartbeat  tick=%d  symbols=%d  intents_emitted=%d  "
            "snapshots_rx=%d  news_rx=%d  regime=%s(%.2f)%s%s%s%s%s",
            tick, syms, emitted, snaps_rx, news_rx,
            _regime.regime, _regime.confidence, _rot_hb, _alloc_hb, _mm_hb, _sc_hb, _tune_hb,
        )
        _active_ai = _agent_all_active()
        if _active_ai:
            log.info("agent_intel active=%d symbols=%s", len(_active_ai), list(_active_ai.keys()))
        for sym, snap in sorted(cache_snapshot.items()):
            if snap is not None:
                rsi_str = f"{snap.rsi14:.1f}" if snap.rsi14 is not None else "n/a"
                log.info(
                    "  %s  last=%.2f  bid=%.2f  ask=%.2f  vwap=%.2f  rsi14=%s",
                    sym, snap.last, snap.bid, snap.ask, snap.vwap, rsi_str,
                )

        # ── Phase B: Dynamic Universe + Rotation Selector ────────────
        now_loop = time.time()
        try:
          if now_loop - _last_rotation_decision_ts >= _ROTATION_DECISION_INTERVAL_S:
            _last_rotation_decision_ts = now_loop
            _sec_scores_b: Dict[str, float] = {}
            _ind_scores_b: Dict[str, float] = {}
            if _ROTATION_SEL_ENABLED or _DYN_UNIVERSE_ENABLED:
                from src.universe.sector_mapper import get_all_sectors as _all_sec, get_all_industries as _all_ind
                for _s in _all_sec():
                    _sec_scores_b[_s] = _get_sector_score(_s)
                for _i in _all_ind():
                    _ind_scores_b[_i] = _get_industry_score(_i)

            if _ROTATION_SEL_ENABLED and _sec_scores_b:
                _rot_d = _compute_rotation_decision(
                    sector_scores=_sec_scores_b,
                    industry_scores=_ind_scores_b,
                    market_mode=_regime.regime,
                )
                _top_sec_str = ", ".join(f"{s}:{sc:.0f}" for s, sc in _rot_d.top_sectors[:5])
                _top_ind_str = ", ".join(f"{i}:{sc:.0f}" for i, sc in _rot_d.top_industries[:5])
                log.info(
                    "sector_rotation_decision top_sectors=[%s] "
                    "rotating_in=%s rotating_out=%s "
                    "top_industries=[%s]",
                    _top_sec_str, _rot_d.rotating_in, _rot_d.rotating_out,
                    _top_ind_str,
                )

            if _DYN_UNIVERSE_ENABLED:
                _dyn = _build_dynamic_universe(
                    sector_scores=_sec_scores_b,
                    industry_scores=_ind_scores_b,
                    market_mode=_regime.regime,
                )
                log.info(
                    "dynamic_universe active=%d priority=%d reduced=%d "
                    "top_sectors=%s top_industries=%s",
                    len(_dyn.active_symbols), len(_dyn.priority_symbols),
                    len(_dyn.reduced_symbols),
                    _dyn.top_sectors[:5], _dyn.top_industries[:5],
                )

                if _SCAN_SCHEDULER_ENABLED:
                    _sched = _build_scan_schedule(_dyn)
                    _sc_counts = _get_schedule_counts()
                    _sample_high = sorted([s for s, p in _sched.items() if p == "HIGH"])[:5]
                    _sample_low = sorted([s for s, p in _sched.items() if p == "LOW"])[:5]
                    log.info(
                        "scan_schedule high=%d normal=%d low=%d "
                        "sample_high=%s sample_low=%s",
                        _sc_counts.get("HIGH", 0),
                        _sc_counts.get("NORMAL", 0),
                        _sc_counts.get("LOW", 0),
                        _sample_high, _sample_low,
                    )
                    # Update per-symbol scan priority in cache
                    with _lock:
                        for _sym, _pri in _sched.items():
                            _sc_entry = _cache.get(_sym)
                            if _sc_entry is not None:
                                _sc_entry.scan_priority = _pri
        except Exception:
            log.exception("phase_b_refresh_error")

        # ── OFF_HOURS board publish (every 30 s) ────────────────────
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

        # ── Candidate board (every 60 s) ─────────────────────────────
        if now_loop - last_candidate_board_ts >= _CANDIDATE_BOARD_INTERVAL_S:
            last_candidate_board_ts = now_loop
            _print_candidate_board()

        _stop_event.wait(settings.heartbeat_interval_s)
        if _stop_event.is_set():
            break

    # Cleanup
    if _bus is not None:
        try:
            _bus.close()
        except Exception:
            pass
    log.info("Signal arm stopped.")


if __name__ == "__main__":
    main()
