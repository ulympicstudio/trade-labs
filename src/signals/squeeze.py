"""
Squeeze Detection — identify symbols building toward explosive moves.

Tracks per-symbol conditions that precede large price expansions:

    - Range compression (Bollinger bandwidth squeeze)
    - Volume spike (RVOL relative to baseline)
    - Intraday range expansion after compression
    - Spread tightness (institutional interest)

Output
------
- squeeze_score: 0–100
- squeeze_state: WATCH / BUILDING / TRIGGERED
- levels: key price levels to watch

This module is designed to run incrementally — call ``update()``
on every MarketSnapshot, then ``get_squeeze()`` when needed.

Future extensions (Phase 2):
    - Short interest (daily cache)
    - Options volume spike / call-put skew
    - IV expansion
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional


# ── Tunables ─────────────────────────────────────────────────────────

_BB_PERIOD = int(os.environ.get("TL_SQUEEZE_BB_PERIOD", "20"))
_BB_NARROW_PCTL = float(os.environ.get("TL_SQUEEZE_BB_NARROW_PCTL", "0.25"))
_RVOL_SPIKE = float(os.environ.get("TL_SQUEEZE_RVOL_SPIKE", "2.0"))
_RANGE_EXPAND_MULT = float(os.environ.get("TL_SQUEEZE_RANGE_EXPAND", "1.5"))
_MAX_BARS = int(os.environ.get("TL_SQUEEZE_MAX_BARS", "60"))
_TRIGGER_THRESHOLD = int(os.environ.get("TL_SQUEEZE_TRIGGER_SCORE", "65"))
_BUILDING_THRESHOLD = int(os.environ.get("TL_SQUEEZE_BUILDING_SCORE", "35"))

# ── Test-mode force override ─────────────────────────────────────────
_FORCE_SQUEEZE_SCORE = int(os.environ.get("TL_TEST_FORCE_SQUEEZE_SCORE", "0"))

WATCH = "WATCH"
BUILDING = "BUILDING"
TRIGGERED = "TRIGGERED"


@dataclass(frozen=True)
class SqueezeResult:
    """Per-symbol squeeze assessment."""

    symbol: str = ""
    squeeze_score: int = 0              # 0–100
    squeeze_state: str = WATCH          # WATCH / BUILDING / TRIGGERED
    bandwidth_pct: float = 0.0          # current BB bandwidth as % of price
    bandwidth_rank: float = 0.0         # percentile rank (0=widest, 1=tightest)
    rvol: float = 0.0                   # relative volume
    range_ratio: float = 0.0           # current range / avg range
    support: float = 0.0               # recent low
    resistance: float = 0.0            # recent high
    reasons: List[str] = field(default_factory=list)


# ── Per-symbol state ─────────────────────────────────────────────────

@dataclass
class _SymbolState:
    closes: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    volumes: Deque[int] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    bandwidths: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    ranges: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    last_update: float = 0.0


_states: Dict[str, _SymbolState] = {}


def _get_state(symbol: str) -> _SymbolState:
    if symbol not in _states:
        _states[symbol] = _SymbolState()
    return _states[symbol]


# ── Public API ───────────────────────────────────────────────────────

def update(
    symbol: str,
    last: float,
    high: float,
    low: float,
    volume: int = 0,
) -> None:
    """Feed a new bar/tick for a symbol.  Call on every snapshot."""
    st = _get_state(symbol)
    st.closes.append(last)
    st.highs.append(high)
    st.lows.append(low)
    st.volumes.append(volume)

    # Bar range
    bar_range = high - low if high > low else 0.001
    st.ranges.append(bar_range)

    # Bollinger bandwidth
    if len(st.closes) >= _BB_PERIOD:
        window = list(st.closes)[-_BB_PERIOD:]
        mean = sum(window) / _BB_PERIOD
        variance = sum((x - mean) ** 2 for x in window) / _BB_PERIOD
        std = variance ** 0.5
        bandwidth = (2 * std) / mean if mean > 0 else 0.0
        st.bandwidths.append(bandwidth)

    st.last_update = time.time()


def get_squeeze(symbol: str) -> SqueezeResult:
    """Compute squeeze score for a symbol from accumulated data."""
    # ── Test-mode override ─────────────────────────────────────────
    if _FORCE_SQUEEZE_SCORE > 0:
        state = TRIGGERED if _FORCE_SQUEEZE_SCORE >= _TRIGGER_THRESHOLD else (
            BUILDING if _FORCE_SQUEEZE_SCORE >= _BUILDING_THRESHOLD else WATCH)
        return SqueezeResult(
            symbol=symbol,
            squeeze_score=_FORCE_SQUEEZE_SCORE,
            squeeze_state=state,
            reasons=[f"test_force_squeeze={_FORCE_SQUEEZE_SCORE}"],
        )

    st = _get_state(symbol)

    if len(st.closes) < _BB_PERIOD:
        return SqueezeResult(
            symbol=symbol,
            squeeze_state=WATCH,
            reasons=["insufficient_data"],
        )

    score = 0.0
    reasons: List[str] = []
    price = st.closes[-1] if st.closes[-1] > 0 else 1.0

    # ── 1. Bandwidth rank (0–35 pts) ────────────────────────────────
    bw_list = list(st.bandwidths)
    if len(bw_list) >= 5:
        current_bw = bw_list[-1]
        sorted_bw = sorted(bw_list)
        # Rank: 0 = widest, 1 = tightest (inverted because tight = squeeze)
        rank_idx = sorted_bw.index(current_bw)
        rank = 1.0 - (rank_idx / max(len(sorted_bw) - 1, 1))

        pts = 35.0 * rank
        score += pts
        reasons.append(f"bw_rank={rank:.2f}→{pts:.0f}pts")
    else:
        rank = 0.5
        current_bw = 0.0

    # ── 2. RVOL (0–25 pts) ──────────────────────────────────────────
    vol_list = list(st.volumes)
    rvol = 0.0
    if len(vol_list) >= 5 and vol_list[-1] > 0:
        baseline_vol = sum(vol_list[:-1]) / max(len(vol_list) - 1, 1)
        if baseline_vol > 0:
            rvol = vol_list[-1] / baseline_vol
            if rvol >= _RVOL_SPIKE:
                pts = min(25.0, 25.0 * (rvol / (_RVOL_SPIKE * 2)))
                score += pts
                reasons.append(f"rvol={rvol:.1f}→{pts:.0f}pts")

    # ── 3. Range expansion (0–25 pts) ───────────────────────────────
    range_list = list(st.ranges)
    range_ratio = 0.0
    if len(range_list) >= 5:
        avg_range = sum(range_list[:-1]) / max(len(range_list) - 1, 1)
        if avg_range > 0:
            range_ratio = range_list[-1] / avg_range
            if range_ratio >= _RANGE_EXPAND_MULT:
                pts = min(25.0, 25.0 * (range_ratio / (_RANGE_EXPAND_MULT * 2)))
                score += pts
                reasons.append(f"range_ratio={range_ratio:.1f}→{pts:.0f}pts")

    # ── 4. Tight compression bonus (0–15 pts) ───────────────────────
    # If bandwidth is in bottom quartile AND range just expanded → extra
    if rank > 0.75 and range_ratio > 1.0:
        bonus = 15.0 * rank * min(1.0, range_ratio / _RANGE_EXPAND_MULT)
        score += bonus
        reasons.append(f"compression_breakout→{bonus:.0f}pts")

    # ── Final ────────────────────────────────────────────────────────
    squeeze_score = max(0, min(100, int(round(score))))

    if squeeze_score >= _TRIGGER_THRESHOLD:
        state = TRIGGERED
    elif squeeze_score >= _BUILDING_THRESHOLD:
        state = BUILDING
    else:
        state = WATCH

    # Key levels
    high_list = list(st.highs)
    low_list = list(st.lows)
    resistance = max(high_list[-_BB_PERIOD:]) if len(high_list) >= _BB_PERIOD else price * 1.01
    support = min(low_list[-_BB_PERIOD:]) if len(low_list) >= _BB_PERIOD else price * 0.99

    return SqueezeResult(
        symbol=symbol,
        squeeze_score=squeeze_score,
        squeeze_state=state,
        bandwidth_pct=round(current_bw * 100, 4) if current_bw else 0.0,
        bandwidth_rank=round(rank, 3),
        rvol=round(rvol, 2),
        range_ratio=round(range_ratio, 2),
        support=round(support, 2),
        resistance=round(resistance, 2),
        reasons=reasons,
    )


def get_watchlist(min_score: int = 30, max_results: int = 10) -> List[SqueezeResult]:
    """Return top N symbols by squeeze_score across all tracked symbols."""
    results = []
    for sym in list(_states.keys()):
        sq = get_squeeze(sym)
        if sq.squeeze_score >= min_score:
            results.append(sq)
    results.sort(key=lambda r: r.squeeze_score, reverse=True)
    return results[:max_results]
