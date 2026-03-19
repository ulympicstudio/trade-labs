"""Volatility Leadership Engine — per-symbol intraday vol tracking.

Tracks each symbol's volatility signature (relative volume, ATR expansion,
spread quality, compression→expansion) and scores it 0‑100.  The score
drives a five-state machine:

    QUIET → WATCH → BUILDING → TRIGGERED → EXHAUSTED

Only **stock** symbols are scored.  Index ETFs (SPY, QQQ, IWM, DIA) are
used as breadth inputs but never emitted as leaders.

Env toggles
-----------
TL_VOL_ENABLED          master on/off (default: true)
TL_VOL_MIN_SCORE        minimum leader_score to advance beyond WATCH (65)
TL_VOL_MIN_RVOL         relative‑volume floor (1.5)
TL_VOL_MIN_ATRX         ATR‑expansion floor (1.3)
TL_VOL_MAX_SPREAD       max bid-ask spread % (0.003)
TL_VOL_CONFIDENCE_BOOST confidence boost applied to TRIGGERED leaders (0.10)

Force-path overrides (testing)
------------------------------
TL_VOL_FORCE_SCORE      override leader_score for *all* symbols
TL_VOL_FORCE_STATE      override leader_state for *all* symbols
TL_VOL_FORCE_RVOL       override rvol_ratio
TL_VOL_FORCE_ATRX       override atr_expansion_ratio
TL_VOL_FORCE_SYMBOL     restrict forced overrides to this single symbol
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Deque, Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

log = get_logger("volatility_leaders")

# ── Tunables ────────────────────────────────────────────────────────
VOL_ENABLED = os.environ.get("TL_VOL_ENABLED", "true").lower() in ("1", "true", "yes")

_MIN_SCORE       = int(os.environ.get("TL_VOL_MIN_SCORE", "65"))
_MIN_RVOL        = float(os.environ.get("TL_VOL_MIN_RVOL", "1.5"))
_MIN_ATRX        = float(os.environ.get("TL_VOL_MIN_ATRX", "1.3"))
_MAX_SPREAD      = float(os.environ.get("TL_VOL_MAX_SPREAD", "0.003"))
_CONFIDENCE_BOOST = float(os.environ.get("TL_VOL_CONFIDENCE_BOOST", "0.10"))

# Force-path overrides
_FORCE_SCORE     = int(os.environ.get("TL_VOL_FORCE_SCORE", "0"))
_FORCE_STATE     = os.environ.get("TL_VOL_FORCE_STATE", "")
_FORCE_RVOL      = float(os.environ.get("TL_VOL_FORCE_RVOL", "0"))
_FORCE_ATRX      = float(os.environ.get("TL_VOL_FORCE_ATRX", "0"))
_FORCE_SYMBOL    = os.environ.get("TL_VOL_FORCE_SYMBOL", "")

# Internal
_LOOKBACK        = int(os.environ.get("TL_VOL_LOOKBACK", "20"))  # snapshot lookback
_EXHAUSTION_BARS = int(os.environ.get("TL_VOL_EXHAUSTION_BARS", "8"))
_INDEX_ETFS      = frozenset({"SPY", "QQQ", "IWM", "DIA"})


# ── Enums & dataclasses ────────────────────────────────────────────

class LeaderState(str, Enum):
    QUIET     = "QUIET"
    WATCH     = "WATCH"
    BUILDING  = "BUILDING"
    TRIGGERED = "TRIGGERED"
    EXHAUSTED = "EXHAUSTED"


@dataclass(frozen=True)
class VolatilityLeaderResult:
    """Immutable snapshot of a symbol's volatility leadership status."""
    symbol: str
    leader_score: int = 0                   # 0‑100 composite
    leader_state: str = "QUIET"            # state machine value
    rvol_ratio: float = 0.0                 # relative volume vs lookback avg
    atr_expansion_ratio: float = 0.0        # current ATR / baseline ATR
    spread_pct: float = 0.0                 # bid-ask spread %
    range_expansion_pct: float = 0.0        # intraday range vs baseline
    compression_releasing: bool = False     # squeeze→expansion detected
    confidence_boost: float = 0.0           # extra confidence for TRIGGERED
    reasons: List[str] = field(default_factory=list)


@dataclass
class _SymbolVolState:
    """Mutable per-symbol tracking state."""
    last_prices: Deque[float] = field(default_factory=lambda: deque(maxlen=_LOOKBACK))
    last_volumes: Deque[int] = field(default_factory=lambda: deque(maxlen=_LOOKBACK))
    last_atrs: Deque[float] = field(default_factory=lambda: deque(maxlen=_LOOKBACK))
    last_spreads: Deque[float] = field(default_factory=lambda: deque(maxlen=_LOOKBACK))
    state: LeaderState = LeaderState.QUIET
    triggered_ts: float = 0.0
    triggered_count: int = 0     # bars since TRIGGERED
    last_score: int = 0


# ── Module state ────────────────────────────────────────────────────

_states: Dict[str, _SymbolVolState] = {}


# ── Pure helpers ────────────────────────────────────────────────────

def _safe_mean(vals: Deque[float]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _safe_mean_int(vals: Deque[int]) -> float:
    if not vals:
        return 0.0
    return sum(vals) / len(vals)


def _compute_rvol(current_vol: int, hist_vols: Deque[int]) -> float:
    """Relative volume = current / average(lookback)."""
    avg = _safe_mean_int(hist_vols)
    if avg <= 0:
        return 0.0
    return round(current_vol / avg, 2)


def _compute_atr_expansion(current_atr: float, hist_atrs: Deque[float]) -> float:
    """ATR expansion ratio = current ATR / baseline ATR."""
    baseline = _safe_mean(hist_atrs)
    if baseline <= 0:
        return 0.0
    return round(current_atr / baseline, 2)


def _compute_spread_pct(bid: float, ask: float, last: float) -> float:
    if last <= 0 or bid <= 0 or ask <= 0:
        return 0.0
    return round((ask - bid) / last, 5)


def _compute_range_expansion(prices: Deque[float], current_atr: float) -> float:
    """Intraday high-low range as fraction of ATR."""
    if len(prices) < 2 or current_atr <= 0:
        return 0.0
    hi = max(prices)
    lo = min(prices)
    return round((hi - lo) / current_atr, 2) if current_atr > 0 else 0.0


def _detect_compression_release(spreads: Deque[float], atrs: Deque[float]) -> bool:
    """Detect squeeze→expansion: recent ATR rising after period of narrowing."""
    if len(atrs) < 6:
        return False
    older = list(atrs)[:len(atrs) // 2]
    newer = list(atrs)[len(atrs) // 2:]
    avg_older = sum(older) / len(older) if older else 0
    avg_newer = sum(newer) / len(newer) if newer else 0
    return avg_newer > avg_older * 1.15  # 15% expansion over baseline


def _compute_leader_score(
    rvol: float, atrx: float, spread: float,
    range_exp: float, compression: bool,
    regime: str, sector_state: str,
) -> Tuple[int, List[str]]:
    """Compute composite 0‑100 volatility leadership score.

    Weights (total=100):
      RVOL          25   (relative volume)
      ATR expansion 25   (range expansion vs baseline)
      Spread        15   (tighter = better)
      Range expand  15   (absolute intraday range)
      Compression   10   (squeeze release bonus)
      Regime align  5    (bonus in TREND_UP)
      Sector align  5    (bonus in BULLISH/HOT)
    """
    reasons: List[str] = []
    score = 0.0

    # RVOL component (0-25): rvol of 1.5 → 15, 3.0 → 25
    rvol_pts = min(25.0, max(0.0, (rvol - 1.0) * 12.5))
    score += rvol_pts
    if rvol_pts >= 10:
        reasons.append(f"rvol={rvol:.1f}")

    # ATR expansion (0-25): atrx of 1.3 → 15, 2.5 → 25
    atrx_pts = min(25.0, max(0.0, (atrx - 1.0) * 16.7))
    score += atrx_pts
    if atrx_pts >= 10:
        reasons.append(f"atrx={atrx:.1f}")

    # Spread quality (0-15): tighter → higher
    if spread > 0:
        spread_pts = max(0.0, 15.0 - (spread / _MAX_SPREAD) * 15.0)
    else:
        spread_pts = 0.0
    score += spread_pts

    # Range expansion (0-15): more intraday range → higher
    range_pts = min(15.0, max(0.0, range_exp * 5.0))
    score += range_pts
    if range_pts >= 5:
        reasons.append(f"range={range_exp:.1f}")

    # Compression release bonus (0-10)
    if compression:
        score += 10.0
        reasons.append("squeeze_release")

    # Regime alignment (0-5)
    if regime in ("TREND_UP",):
        score += 5.0
        reasons.append("regime_up")
    elif regime in ("CHOP", "PANIC"):
        score -= 2.0

    # Sector alignment (0-5)
    if sector_state in ("BULLISH", "HOT"):
        score += 5.0
        reasons.append(f"sector_{sector_state}")
    elif sector_state in ("BEARISH", "COLD"):
        score -= 2.0

    return max(0, min(100, int(round(score)))), reasons


def _advance_state(
    current: LeaderState, score: int, rvol: float, atrx: float,
    spread: float, triggered_count: int,
) -> LeaderState:
    """State machine transitions."""
    # Exhaustion: been TRIGGERED too long
    if current == LeaderState.TRIGGERED and triggered_count >= _EXHAUSTION_BARS:
        return LeaderState.EXHAUSTED

    # EXHAUSTED stays until score drops
    if current == LeaderState.EXHAUSTED:
        if score < _MIN_SCORE * 0.6:
            return LeaderState.QUIET
        return LeaderState.EXHAUSTED

    # Forward transitions based on score thresholds
    if score >= _MIN_SCORE and rvol >= _MIN_RVOL and atrx >= _MIN_ATRX and spread <= _MAX_SPREAD:
        return LeaderState.TRIGGERED

    if score >= _MIN_SCORE * 0.8 and rvol >= _MIN_RVOL * 0.8:
        if current.value in (LeaderState.QUIET, LeaderState.WATCH):
            return LeaderState.BUILDING
        return current  # keep BUILDING or higher

    if score >= _MIN_SCORE * 0.5:
        if current == LeaderState.QUIET:
            return LeaderState.WATCH
        return current  # don't regress once BUILDING+

    # Score dropped — regress
    if current in (LeaderState.BUILDING, LeaderState.WATCH):
        return LeaderState.QUIET
    return current


# ── Public API ──────────────────────────────────────────────────────

def update_volatility(
    symbol: str, last: float, bid: float, ask: float,
    volume: int, atr: float, rvol_snap: Optional[float],
) -> None:
    """Feed a new market snapshot into the volatility tracker.

    Called from ``signal_main._on_snapshot`` for every tick.
    """
    if not VOL_ENABLED:
        return

    if symbol in _INDEX_ETFS:
        return  # track but don't score index ETFs

    vs = _states.setdefault(symbol, _SymbolVolState())
    if last > 0:
        vs.last_prices.append(last)
    if volume > 0:
        vs.last_volumes.append(volume)
    if atr > 0:
        vs.last_atrs.append(atr)
    spread = _compute_spread_pct(bid, ask, last)
    if spread > 0:
        vs.last_spreads.append(spread)


def compute_leader(
    symbol: str,
    regime: str = "",
    sector_state: str = "",
) -> VolatilityLeaderResult:
    """Compute the current volatility leader result for *symbol*.

    Called from ``signal_main._evaluate`` after event-score computation.
    Returns an immutable :class:`VolatilityLeaderResult`.
    """
    if not VOL_ENABLED or symbol in _INDEX_ETFS:
        return VolatilityLeaderResult(symbol=symbol)

    vs = _states.get(symbol)
    if vs is None or len(vs.last_prices) < 3:
        return VolatilityLeaderResult(symbol=symbol)

    # ── Raw metrics ──────────────────────────────────────────────
    current_vol = vs.last_volumes[-1] if vs.last_volumes else 0
    rvol = _compute_rvol(current_vol, vs.last_volumes)
    current_atr = vs.last_atrs[-1] if vs.last_atrs else 0.0
    atrx = _compute_atr_expansion(current_atr, vs.last_atrs)
    spread = vs.last_spreads[-1] if vs.last_spreads else 0.0
    range_exp = _compute_range_expansion(vs.last_prices, current_atr)
    compression = _detect_compression_release(vs.last_spreads, vs.last_atrs)

    # ── Force-path overrides ─────────────────────────────────────
    apply_force = not _FORCE_SYMBOL or _FORCE_SYMBOL == symbol
    if _FORCE_RVOL > 0 and apply_force:
        rvol = _FORCE_RVOL
    if _FORCE_ATRX > 0 and apply_force:
        atrx = _FORCE_ATRX

    # ── Score ────────────────────────────────────────────────────
    score, reasons = _compute_leader_score(
        rvol, atrx, spread, range_exp, compression, regime, sector_state,
    )
    if _FORCE_SCORE > 0 and apply_force:
        score = _FORCE_SCORE
        reasons = reasons + [f"force_score={_FORCE_SCORE}"]

    # ── State machine ────────────────────────────────────────────
    prev_state = vs.state
    if _FORCE_STATE and apply_force:
        try:
            vs.state = LeaderState(_FORCE_STATE)
        except ValueError:
            pass  # ignore invalid force state
    else:
        vs.state = _advance_state(
            vs.state, score, rvol, atrx, spread, vs.triggered_count,
        )

    # Track triggered duration
    if vs.state == LeaderState.TRIGGERED:
        vs.triggered_count += 1
    else:
        vs.triggered_count = 0

    vs.last_score = score

    # Log state transitions
    if vs.state != prev_state:
        log.info(
            "vol_state_change symbol=%s %s→%s score=%d rvol=%.1f atrx=%.1f",
            symbol, prev_state.value, vs.state.value, score, rvol, atrx,
        )

    conf_boost = _CONFIDENCE_BOOST if vs.state == LeaderState.TRIGGERED else 0.0

    return VolatilityLeaderResult(
        symbol=symbol,
        leader_score=score,
        leader_state=vs.state.value,
        rvol_ratio=rvol,
        atr_expansion_ratio=atrx,
        spread_pct=spread,
        range_expansion_pct=range_exp,
        compression_releasing=compression,
        confidence_boost=conf_boost,
        reasons=reasons,
    )


def get_top_leaders(n: int = 5) -> List[VolatilityLeaderResult]:
    """Return the top *n* symbols ranked by leader_score (TRIGGERED first).

    Used by monitor_main for the volatility summary table.
    """
    results: List[Tuple[int, str, _SymbolVolState]] = []
    for sym, vs in _states.items():
        if sym in _INDEX_ETFS:
            continue
        # Rank: TRIGGERED first (bonus 200), then score
        bonus = 200 if vs.state == LeaderState.TRIGGERED else 0
        bonus += 100 if vs.state == LeaderState.BUILDING else 0
        results.append((bonus + vs.last_score, sym, vs))

    results.sort(key=lambda x: -x[0])
    out: List[VolatilityLeaderResult] = []
    for _, sym, vs in results[:n]:
        out.append(VolatilityLeaderResult(
            symbol=sym,
            leader_score=vs.last_score,
            leader_state=vs.state.value,
            rvol_ratio=_compute_rvol(
                vs.last_volumes[-1] if vs.last_volumes else 0, vs.last_volumes),
            atr_expansion_ratio=_compute_atr_expansion(
                vs.last_atrs[-1] if vs.last_atrs else 0.0, vs.last_atrs),
            spread_pct=vs.last_spreads[-1] if vs.last_spreads else 0.0,
        ))
    return out


def get_leader_summary() -> str:
    """One-line summary for heartbeat logs: top=[NVDA:78(TRIGGERED), ...]."""
    leaders = get_top_leaders(5)
    if not leaders:
        return "top=[]"
    parts = [f"{r.symbol}:{r.leader_score}({r.leader_state})" for r in leaders]
    return f"top=[{', '.join(parts)}]"
