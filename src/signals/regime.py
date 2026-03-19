"""
Regime Detection — classify market state from index data.

Uses SPY/QQQ snapshots to determine the current market regime:

    TREND_UP   — strong uptrend (EMA slope positive, ATR moderate)
    TREND_DOWN — strong downtrend (EMA slope negative, ATR rising)
    CHOP       — no clear trend (flat slope, low ADX proxy)
    PANIC      — high-vol selloff (ATR spike, price below EMA)

Signal arm uses regime to gate strategies:
    - RSI mean-reversion only in CHOP / low-vol
    - Momentum only in TREND_UP / TREND_DOWN
    - News-consensus trades allowed in any regime (but sized by regime)

All thresholds are env-overridable for tuning.
"""

from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, List, Optional


# ── Tunables ─────────────────────────────────────────────────────────

_EMA_FAST = int(os.environ.get("TL_REGIME_EMA_FAST", "20"))
_EMA_SLOW = int(os.environ.get("TL_REGIME_EMA_SLOW", "50"))
_ATR_LOOKBACK = int(os.environ.get("TL_REGIME_ATR_LOOKBACK", "14"))
_SLOPE_THRESHOLD = float(os.environ.get("TL_REGIME_SLOPE_THRESHOLD", "0.001"))
_ATR_PANIC_MULT = float(os.environ.get("TL_REGIME_ATR_PANIC_MULT", "2.0"))
_REGIME_INDEX = os.environ.get("TL_REGIME_INDEX", "SPY")
_MAX_BARS = int(os.environ.get("TL_REGIME_MAX_BARS", "100"))

# Volatility regime thresholds (ATR% relative to baseline)
_VOL_HIGH_MULT = float(os.environ.get("TL_REGIME_VOL_HIGH_MULT", "1.5"))
_VOL_LOW_MULT = float(os.environ.get("TL_REGIME_VOL_LOW_MULT", "0.6"))

# ── Test-mode force overrides ────────────────────────────────────────
_FORCE_REGIME = os.environ.get("TL_TEST_FORCE_REGIME", "").upper()  # e.g. PANIC
_FORCE_ATR_SPIKE = os.environ.get(
    "TL_TEST_FORCE_ATR_SPIKE", "false"
).lower() in ("1", "true", "yes")

# ── Result ───────────────────────────────────────────────────────────

TREND_UP = "TREND_UP"
TREND_DOWN = "TREND_DOWN"
CHOP = "CHOP"
PANIC = "PANIC"

# Volatility regime labels
VOL_LOW = "LOW"
VOL_NORMAL = "NORMAL"
VOL_HIGH = "HIGH"

# Strategy gating: which strategies are allowed per regime
STRATEGY_GATE = {
    TREND_UP:   {"momentum", "consensus_news", "breakout"},
    TREND_DOWN: {"momentum", "consensus_news"},
    CHOP:       {"mean_revert_rsi", "consensus_news"},
    PANIC:      {"consensus_news"},
}

# Risk multiplier: scale position size by regime
RISK_MULT = {
    TREND_UP:   1.0,
    TREND_DOWN: 0.7,
    CHOP:       0.5,
    PANIC:      0.3,
}


@dataclass(frozen=True)
class RegimeState:
    """Snapshot of the current market regime."""

    regime: str = CHOP                  # TREND_UP / TREND_DOWN / CHOP / PANIC
    confidence: float = 0.5             # 0..1 how confident we are
    ema_fast: float = 0.0               # current fast EMA
    ema_slow: float = 0.0               # current slow EMA
    slope: float = 0.0                  # fast EMA slope (pct per bar)
    atr_pct: float = 0.0               # ATR as % of price
    atr_baseline_pct: float = 0.0       # rolling baseline ATR %
    vol_regime: str = VOL_NORMAL        # LOW / NORMAL / HIGH
    reasons: List[str] = field(default_factory=list)

    @property
    def risk_mult(self) -> float:
        return RISK_MULT.get(self.regime, 0.5)

    def allows_strategy(self, strategy_name: str) -> bool:
        return strategy_name in STRATEGY_GATE.get(self.regime, set())


# ── Per-index rolling state ──────────────────────────────────────────

@dataclass
class _IndexState:
    closes: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    highs: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    lows: Deque[float] = field(default_factory=lambda: deque(maxlen=_MAX_BARS))
    ema_fast: float = 0.0
    ema_slow: float = 0.0
    ema_fast_prev: float = 0.0
    atr_vals: Deque[float] = field(default_factory=lambda: deque(maxlen=_ATR_LOOKBACK * 3))
    last_update: float = 0.0


_states: Dict[str, _IndexState] = {}
_last_regime: RegimeState = RegimeState()


def _get_state(symbol: str) -> _IndexState:
    if symbol not in _states:
        _states[symbol] = _IndexState()
    return _states[symbol]


# ── EMA helpers ──────────────────────────────────────────────────────

def _ema_update(prev: float, value: float, period: int) -> float:
    if prev == 0.0:
        return value
    k = 2.0 / (period + 1)
    return value * k + prev * (1 - k)


# ── Public API ───────────────────────────────────────────────────────

def update_index(symbol: str, last: float, high: float, low: float) -> None:
    """Feed a new bar/tick for an index symbol.  Call on every snapshot."""
    st = _get_state(symbol)
    st.closes.append(last)
    st.highs.append(high)
    st.lows.append(low)
    st.ema_fast_prev = st.ema_fast
    st.ema_fast = _ema_update(st.ema_fast, last, _EMA_FAST)
    st.ema_slow = _ema_update(st.ema_slow, last, _EMA_SLOW)

    # True Range
    if len(st.closes) >= 2:
        prev_close = st.closes[-2]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
    else:
        tr = high - low if high > low else 0.01
    st.atr_vals.append(tr)
    st.last_update = time.time()


def get_regime(symbol: str = "") -> RegimeState:
    """Compute current regime from the tracked index.

    Parameters
    ----------
    symbol:
        Override index symbol.  Defaults to ``TL_REGIME_INDEX`` (SPY).
    """
    global _last_regime

    # ── Test-mode override: force a specific regime ───────────────
    if _FORCE_REGIME in (TREND_UP, TREND_DOWN, CHOP, PANIC):
        vol = VOL_HIGH if _FORCE_ATR_SPIKE else VOL_NORMAL
        forced = RegimeState(
            regime=_FORCE_REGIME,
            confidence=0.90,
            atr_pct=0.03 if _FORCE_ATR_SPIKE else 0.012,
            atr_baseline_pct=0.012,
            vol_regime=vol,
            reasons=[f"test_force_regime={_FORCE_REGIME}"],
        )
        _last_regime = forced
        return forced

    idx = symbol or _REGIME_INDEX
    st = _get_state(idx)

    # Not enough data yet — return safe default
    if len(st.closes) < max(_EMA_FAST, _EMA_SLOW, _ATR_LOOKBACK):
        return RegimeState(
            regime=CHOP,
            confidence=0.3,
            reasons=["insufficient_data"],
        )

    # ATR %
    atr_list = list(st.atr_vals)
    current_atr = sum(atr_list[-_ATR_LOOKBACK:]) / _ATR_LOOKBACK if len(atr_list) >= _ATR_LOOKBACK else sum(atr_list) / max(len(atr_list), 1)
    price = st.closes[-1] if st.closes[-1] > 0 else 1.0
    atr_pct = current_atr / price

    # Baseline ATR % (full history average)
    baseline_atr = sum(atr_list) / max(len(atr_list), 1)
    baseline_pct = baseline_atr / price

    # EMA slope (pct change per bar)
    slope = 0.0
    if st.ema_fast_prev > 0:
        slope = (st.ema_fast - st.ema_fast_prev) / st.ema_fast_prev

    # ── Classify ─────────────────────────────────────────────────────
    reasons: List[str] = []
    regime = CHOP
    confidence = 0.5

    # Check PANIC first (overrides everything)
    if atr_pct > baseline_pct * _ATR_PANIC_MULT and st.closes[-1] < st.ema_slow:
        regime = PANIC
        confidence = min(0.95, 0.6 + (atr_pct / baseline_pct - _ATR_PANIC_MULT) * 0.1)
        reasons.append(f"atr_spike={atr_pct:.4f}>{baseline_pct:.4f}*{_ATR_PANIC_MULT}")
        reasons.append("price_below_slow_ema")

    elif abs(slope) >= _SLOPE_THRESHOLD and st.ema_fast > st.ema_slow:
        regime = TREND_UP
        confidence = min(0.95, 0.5 + abs(slope) / _SLOPE_THRESHOLD * 0.15)
        reasons.append(f"slope={slope:.4f}")
        reasons.append("fast_above_slow")

    elif abs(slope) >= _SLOPE_THRESHOLD and st.ema_fast < st.ema_slow:
        regime = TREND_DOWN
        confidence = min(0.95, 0.5 + abs(slope) / _SLOPE_THRESHOLD * 0.15)
        reasons.append(f"slope={slope:.4f}")
        reasons.append("fast_below_slow")

    else:
        regime = CHOP
        confidence = max(0.3, 0.7 - abs(slope) / _SLOPE_THRESHOLD * 0.2)
        reasons.append(f"slope_flat={slope:.4f}")

    _last_regime = RegimeState(
        regime=regime,
        confidence=round(confidence, 3),
        ema_fast=round(st.ema_fast, 4),
        ema_slow=round(st.ema_slow, 4),
        slope=round(slope, 6),
        atr_pct=round(atr_pct, 6),
        atr_baseline_pct=round(baseline_pct, 6),
        vol_regime=_classify_vol(atr_pct, baseline_pct),
        reasons=reasons,
    )
    return _last_regime


def last_regime() -> RegimeState:
    """Return the most recently computed regime (no recomputation)."""
    return _last_regime


def _classify_vol(atr_pct: float, baseline_pct: float) -> str:
    """Classify volatility regime from ATR % vs baseline."""
    if baseline_pct <= 0:
        return VOL_NORMAL
    ratio = atr_pct / baseline_pct
    if ratio >= _VOL_HIGH_MULT:
        return VOL_HIGH
    if ratio <= _VOL_LOW_MULT:
        return VOL_LOW
    return VOL_NORMAL
