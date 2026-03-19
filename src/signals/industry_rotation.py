"""Industry Rotation Engine — industry-level relative-strength & rotation scoring.

Tracks industries inside sectors and produces a rotation_score (0-100)
that influences signal prioritisation and risk sizing.

The engine maintains per-industry rolling state from incoming snapshot,
news, and volatility data, then provides rotation scores and capital-tilt
signals.

Lifecycle
---------
1. ``update_industry_rotation(symbol, price)`` — feed every snapshot.
2. ``update_industry_news(symbol, impact_score)`` — feed every news event.
3. ``update_industry_volatility(symbol)`` — feed when vol leader detected.
4. ``compute_industry_rotation(symbol)`` — returns IndustryRotationResult.
5. ``get_top_industries(n)`` — top N industries for monitor display.
6. ``get_rotation_summary()`` — one-liner for heartbeat.

All state is module-level (single process) — no external deps.

Env toggles
-----------
TL_ROTATION_ENABLED           master on/off (default true)
TL_ROTATION_LOOKBACK          rolling price lookback (default 20)
TL_ROTATION_NEWS_DECAY_S      news heat half-life seconds (default 1800)

Force-path overrides (testing)
------------------------------
TL_TEST_FORCE_INDUSTRY        force specific industry name
TL_TEST_FORCE_ROTATION_STATE  force rotation state
TL_TEST_FORCE_ROTATION_SCORE  force rotation score
"""

from __future__ import annotations

import math
import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

from src.universe.sector_mapper import classify_symbol as _classify_symbol
from src.monitoring.logger import get_logger

log = get_logger("industry_rotation")

# ── Tunables ────────────────────────────────────────────────────────
ROTATION_ENABLED = os.environ.get(
    "TL_ROTATION_ENABLED", "true"
).lower() in ("1", "true", "yes")

_LOOKBACK = int(os.environ.get("TL_ROTATION_LOOKBACK", "20"))
_NEWS_DECAY_S = float(os.environ.get("TL_ROTATION_NEWS_DECAY_S", "1800"))

# Force-path overrides
_FORCE_INDUSTRY = os.environ.get("TL_TEST_FORCE_INDUSTRY", "").strip()
_FORCE_ROTATION_STATE = os.environ.get("TL_TEST_FORCE_ROTATION_STATE", "").strip()
_FORCE_ROTATION_SCORE = int(os.environ.get("TL_TEST_FORCE_ROTATION_SCORE", "0"))

if _FORCE_INDUSTRY and _FORCE_ROTATION_STATE:
    log.warning(
        "FORCE-INDUSTRY-ROTATION ACTIVE industry=%s state=%s score=%d",
        _FORCE_INDUSTRY, _FORCE_ROTATION_STATE, _FORCE_ROTATION_SCORE,
    )


# ── Enums & dataclasses ────────────────────────────────────────────

class RotationState(str, Enum):
    COLD = "COLD"
    NEUTRAL = "NEUTRAL"
    ROTATING_IN = "ROTATING_IN"
    LEADING = "LEADING"
    OVERBOUGHT = "OVERBOUGHT"
    ROTATING_OUT = "ROTATING_OUT"


@dataclass(frozen=True)
class IndustryRotationResult:
    """Immutable snapshot of an industry's rotation status."""
    sector: str = "UNKNOWN"
    industry: str = "UNKNOWN"
    rotation_score: int = 0           # 0-100 composite
    rotation_state: str = "NEUTRAL"   # RotationState value
    relative_strength: float = 0.0    # industry return vs sector avg
    breadth: float = 0.0              # pct of symbols rising
    news_heat: float = 0.0            # decayed news impact
    vol_leaders: int = 0              # count of vol-leader symbols
    symbols_tracked: int = 0          # number of symbols in industry


# ── Internal per-industry tracker ────────────────────────────────────

@dataclass
class _IndustryTracker:
    """Mutable per-industry tracking state."""
    # Per-symbol rolling prices
    sym_prices: Dict[str, Deque[float]] = field(default_factory=dict)
    # News heat: list of (timestamp, impact)
    news_events: Deque[tuple] = field(default_factory=lambda: deque(maxlen=200))
    # Volatility leader hits: list of timestamps
    vol_hits: Deque[float] = field(default_factory=lambda: deque(maxlen=100))
    # State machine
    state: RotationState = RotationState.NEUTRAL
    last_score: int = 0
    last_update_ts: float = 0.0


# ── Module state ────────────────────────────────────────────────────
# _industry_state[sector][industry] = _IndustryTracker
_industry_state: Dict[str, Dict[str, _IndustryTracker]] = {}

# Sector-level average return for relative strength
_sector_returns: Dict[str, Deque[float]] = {}


def _get_tracker(sector: str, industry: str) -> _IndustryTracker:
    """Get or create tracker for a sector/industry pair."""
    if sector not in _industry_state:
        _industry_state[sector] = {}
    if industry not in _industry_state[sector]:
        _industry_state[sector][industry] = _IndustryTracker()
    return _industry_state[sector][industry]


# ── Pure helpers ────────────────────────────────────────────────────

def _sym_return(prices: Deque[float]) -> float:
    """Compute return from oldest to newest price in deque."""
    if len(prices) < 2:
        return 0.0
    oldest = prices[0]
    if oldest <= 0:
        return 0.0
    return (prices[-1] / oldest - 1.0) * 100.0


def _compute_relative_strength(trk: _IndustryTracker, sector: str) -> float:
    """Industry return vs sector average return."""
    # Industry average return
    rets = []
    for prices in trk.sym_prices.values():
        r = _sym_return(prices)
        if r != 0.0 or len(prices) >= 2:
            rets.append(r)
    if not rets:
        return 0.0
    ind_ret = sum(rets) / len(rets)

    # Sector average return (across all industries in sector)
    sector_rets = _sector_returns.get(sector)
    sector_avg = 0.0
    if sector_rets and len(sector_rets) >= 1:
        sector_avg = sum(sector_rets) / len(sector_rets)

    return round(ind_ret - sector_avg, 3)


def _compute_breadth(trk: _IndustryTracker) -> float:
    """Percent of symbols with positive short-term return."""
    if not trk.sym_prices:
        return 50.0
    total = 0
    advancing = 0
    for prices in trk.sym_prices.values():
        if len(prices) >= 2:
            total += 1
            if prices[-1] > prices[-2]:
                advancing += 1
    if total == 0:
        return 50.0
    return round(advancing / total * 100.0, 1)


def _compute_news_heat(trk: _IndustryTracker) -> float:
    """Exponentially-decayed news heat."""
    now = time.time()
    heat = 0.0
    for ts, imp in trk.news_events:
        age = now - ts
        if age < _NEWS_DECAY_S * 4:
            decay = math.exp(-age / _NEWS_DECAY_S) if _NEWS_DECAY_S > 0 else 0.0
            heat += imp * decay
    return round(heat, 2)


def _compute_vol_leaders(trk: _IndustryTracker) -> int:
    """Count recent volatility leader hits (last 5 min)."""
    now = time.time()
    cutoff = now - 300.0
    count = 0
    for ts in trk.vol_hits:
        if ts >= cutoff:
            count += 1
    return count


def _compute_momentum_persistence(trk: _IndustryTracker) -> float:
    """Fraction of recent lookback bars where industry avg was positive.
    Returns 0.0 to 1.0.
    """
    if not trk.sym_prices:
        return 0.0
    # Build aggregate return series from symbol prices
    # Use the last _LOOKBACK snapshots
    max_len = 0
    for prices in trk.sym_prices.values():
        if len(prices) > max_len:
            max_len = len(prices)
    if max_len < 3:
        return 0.0
    window = min(_LOOKBACK, max_len)
    positive_bars = 0
    total_bars = 0
    for i in range(1, window):
        bar_rets = []
        for prices in trk.sym_prices.values():
            if len(prices) > i:
                prev = prices[-(i + 1)]
                curr = prices[-i]
                if prev > 0:
                    bar_rets.append(curr / prev - 1.0)
        if bar_rets:
            total_bars += 1
            if sum(bar_rets) / len(bar_rets) > 0:
                positive_bars += 1
    if total_bars == 0:
        return 0.0
    return round(positive_bars / total_bars, 2)


def _compute_rotation_score(
    rs: float, breadth: float, news_heat: float,
    vol_leaders: int, momentum_persist: float,
) -> Tuple[int, List[str]]:
    """Compute composite 0-100 rotation score.

    Weights (total=100):
      Relative Strength    25
      Breadth              20
      News Heat            15
      Volatility Leaders   20
      Momentum Persistence 20
    """
    reasons: List[str] = []
    score = 0.0

    # Relative Strength (0-25): rs of +0.5% → 12, +1.0% → 25
    rs_pts = min(25.0, max(0.0, rs * 25.0))
    score += rs_pts
    if rs_pts >= 5:
        reasons.append(f"rs={rs:+.2f}")

    # Breadth (0-20): 50% → 0, 75% → 10, 100% → 20
    breadth_pts = min(20.0, max(0.0, (breadth - 50.0) * 0.4))
    score += breadth_pts
    if breadth_pts >= 5:
        reasons.append(f"breadth={breadth:.0f}%")

    # News Heat (0-15): heat 2 → 8, heat 5 → 15
    heat_pts = min(15.0, max(0.0, news_heat * 3.0))
    score += heat_pts
    if heat_pts >= 3:
        reasons.append(f"heat={news_heat:.1f}")

    # Volatility Leaders (0-20): 1 leader → 10, 2+ → 20
    vol_pts = min(20.0, max(0.0, vol_leaders * 10.0))
    score += vol_pts
    if vol_pts >= 5:
        reasons.append(f"vol_leaders={vol_leaders}")

    # Momentum Persistence (0-20): 50% → 10, 100% → 20
    mom_pts = min(20.0, max(0.0, momentum_persist * 20.0))
    score += mom_pts
    if mom_pts >= 5:
        reasons.append(f"momentum={momentum_persist:.0%}")

    return max(0, min(100, int(round(score)))), reasons


def _classify_rotation_state(score: int) -> RotationState:
    """Map score to rotation state."""
    if score > 90:
        return RotationState.ROTATING_OUT
    if score > 80:
        return RotationState.OVERBOUGHT
    if score >= 60:
        return RotationState.LEADING
    if score >= 40:
        return RotationState.ROTATING_IN
    if score >= 20:
        return RotationState.NEUTRAL
    return RotationState.COLD


# ── Public API ──────────────────────────────────────────────────────

def update_industry_rotation(symbol: str, price: float) -> None:
    """Feed a snapshot price into industry rotation tracking.

    Called from signal_main._on_snapshot() for every tick.  O(1) per symbol.
    """
    if not ROTATION_ENABLED or price <= 0:
        return

    profile = _classify_symbol(symbol)
    if profile.sector == "UNKNOWN" or profile.industry == "UNKNOWN":
        return
    # Skip sector ETFs and index ETFs
    if profile.industry == "Sector ETF" or profile.sector == "Index":
        return

    trk = _get_tracker(profile.sector, profile.industry)
    trk.last_update_ts = time.time()

    # Per-symbol rolling price
    if symbol not in trk.sym_prices:
        trk.sym_prices[symbol] = deque(maxlen=_LOOKBACK + 5)
    trk.sym_prices[symbol].append(price)

    # Update sector-level return tracking
    if profile.sector not in _sector_returns:
        _sector_returns[profile.sector] = deque(maxlen=_LOOKBACK + 5)
    # Append this symbol's instant return to sector deque (for averaging)
    sym_dq = trk.sym_prices[symbol]
    if len(sym_dq) >= 2 and sym_dq[-2] > 0:
        inst_ret = (sym_dq[-1] / sym_dq[-2] - 1.0) * 100.0
        _sector_returns[profile.sector].append(inst_ret)


def update_industry_news(symbol: str, impact_score: int = 0) -> None:
    """Feed a news event into industry heat tracking.

    Called from signal_main._on_news() for every news event.  O(1).
    """
    if not ROTATION_ENABLED:
        return

    profile = _classify_symbol(symbol)
    if profile.sector == "UNKNOWN" or profile.industry == "UNKNOWN":
        return

    trk = _get_tracker(profile.sector, profile.industry)
    trk.news_events.append((time.time(), max(1, impact_score)))


def update_industry_volatility(symbol: str) -> None:
    """Record a volatility leader hit for this symbol's industry.

    Called from signal_main when volatility engine tags a leader.  O(1).
    """
    if not ROTATION_ENABLED:
        return

    profile = _classify_symbol(symbol)
    if profile.sector == "UNKNOWN" or profile.industry == "UNKNOWN":
        return

    trk = _get_tracker(profile.sector, profile.industry)
    trk.vol_hits.append(time.time())


def compute_industry_rotation(symbol: str) -> IndustryRotationResult:
    """Compute current rotation result for *symbol*'s industry.

    Called from signal_main._evaluate() each cycle.
    Returns an immutable IndustryRotationResult.
    """
    if not ROTATION_ENABLED:
        return IndustryRotationResult()

    profile = _classify_symbol(symbol)
    if profile.sector == "UNKNOWN" or profile.industry == "UNKNOWN":
        return IndustryRotationResult(sector=profile.sector, industry=profile.industry)

    sector = profile.sector
    industry = profile.industry

    # ── Force-path override ──────────────────────────────────────
    if _FORCE_INDUSTRY and industry == _FORCE_INDUSTRY and _FORCE_ROTATION_STATE:
        forced_state = _FORCE_ROTATION_STATE
        forced_score = _FORCE_ROTATION_SCORE if _FORCE_ROTATION_SCORE > 0 else 75
        trk = _get_tracker(sector, industry)
        # Still advance state machine for logging
        prev_state = trk.state
        try:
            new_state = RotationState(forced_state)
        except ValueError:
            new_state = RotationState.LEADING
        if new_state != prev_state:
            log.info(
                "industry_rotation_state_change sector=%s industry=%s "
                "old=%s new=%s score=%d",
                sector, industry, prev_state.value, new_state.value, forced_score,
            )
            trk.state = new_state
        trk.last_score = forced_score
        return IndustryRotationResult(
            sector=sector,
            industry=industry,
            rotation_score=forced_score,
            rotation_state=new_state.value,
            relative_strength=0.5,
            breadth=75.0,
            news_heat=3.0,
            vol_leaders=2,
            symbols_tracked=len(trk.sym_prices) or 5,
        )

    trk = _get_tracker(sector, industry)
    if not trk.sym_prices:
        return IndustryRotationResult(sector=sector, industry=industry)

    # ── Compute metrics ──────────────────────────────────────────
    rs = _compute_relative_strength(trk, sector)
    breadth = _compute_breadth(trk)
    news_heat = _compute_news_heat(trk)
    vol_leaders = _compute_vol_leaders(trk)
    mom_persist = _compute_momentum_persistence(trk)

    # ── Score ────────────────────────────────────────────────────
    score, reasons = _compute_rotation_score(
        rs, breadth, news_heat, vol_leaders, mom_persist,
    )

    # ── State machine ────────────────────────────────────────────
    prev_state = trk.state
    new_state = _classify_rotation_state(score)

    if new_state != prev_state:
        log.info(
            "industry_rotation_state_change sector=%s industry=%s "
            "old=%s new=%s score=%d",
            sector, industry, prev_state.value, new_state.value, score,
        )
        trk.state = new_state

    trk.last_score = score

    return IndustryRotationResult(
        sector=sector,
        industry=industry,
        rotation_score=score,
        rotation_state=new_state.value,
        relative_strength=rs,
        breadth=breadth,
        news_heat=news_heat,
        vol_leaders=vol_leaders,
        symbols_tracked=len(trk.sym_prices),
    )


def get_top_industries(n: int = 8) -> List[IndustryRotationResult]:
    """Return top *n* industries ranked by rotation_score.

    LEADING/ROTATING_IN first, then by score.
    Used by monitor_main for the rotation summary table.
    """
    results: List[Tuple[int, str, str, _IndustryTracker]] = []
    for sector, industries in _industry_state.items():
        for industry, trk in industries.items():
            bonus = 200 if trk.state == RotationState.LEADING else 0
            bonus += 100 if trk.state == RotationState.ROTATING_IN else 0
            results.append((bonus + trk.last_score, sector, industry, trk))

    results.sort(key=lambda x: -x[0])
    out: List[IndustryRotationResult] = []
    for _, sector, industry, trk in results[:n]:
        rs = _compute_relative_strength(trk, sector)
        breadth = _compute_breadth(trk)
        news_heat = _compute_news_heat(trk)
        vol_leaders = _compute_vol_leaders(trk)
        out.append(IndustryRotationResult(
            sector=sector,
            industry=industry,
            rotation_score=trk.last_score,
            rotation_state=trk.state.value,
            relative_strength=rs,
            breadth=breadth,
            news_heat=news_heat,
            vol_leaders=vol_leaders,
            symbols_tracked=len(trk.sym_prices),
        ))
    return out


def get_rotation_summary() -> str:
    """One-line summary for heartbeat: rotation_leaders=[Semiconductors:72(LEADING), ...]."""
    leaders = get_top_industries(5)
    if not leaders:
        return "rotation_leaders=[]"
    parts = [f"{r.industry}:{r.rotation_score}({r.rotation_state})" for r in leaders]
    return f"rotation_leaders=[{', '.join(parts)}]"


def get_industry_score(industry: str) -> float:
    """Return a 0–100 rotation score for *industry*.

    Used by the composite scoring layer.  Returns 50 if unknown.
    """
    if not ROTATION_ENABLED:
        return 50.0
    for sector, industries in _industry_state.items():
        trk = industries.get(industry)
        if trk is not None:
            return float(trk.last_score)
    return 50.0


def get_rotation_state_bonus(state: str) -> int:
    """Return event-score bonus points for a rotation state.

    LEADING    → +6
    ROTATING_IN → +4
    ROTATING_OUT → -4
    """
    _BONUS = {
        "LEADING": 6,
        "ROTATING_IN": 4,
        "OVERBOUGHT": 0,
        "ROTATING_OUT": -4,
        "COLD": -2,
        "NEUTRAL": 0,
    }
    return _BONUS.get(state, 0)


def get_risk_qty_multiplier(state: str) -> float:
    """Return qty multiplier for risk sizing based on rotation state.

    LEADING       → 1.2
    ROTATING_IN   → 1.1
    ROTATING_OUT  → 0.75
    COLD          → 0.5
    """
    _MULT = {
        "LEADING": 1.2,
        "ROTATING_IN": 1.1,
        "OVERBOUGHT": 0.9,
        "ROTATING_OUT": 0.75,
        "COLD": 0.5,
        "NEUTRAL": 1.0,
    }
    return _MULT.get(state, 1.0)
