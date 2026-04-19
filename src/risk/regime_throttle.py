"""Regime × Session dynamic throttle — replaces hard regime gates.

Instead of blocking intents/orders in adverse regimes, this module
provides continuous multipliers for score, position cap, and max
simultaneous positions.  A ``probing_mode`` allows micro-size trades
even in worst regimes so outcome data can be collected.

Usage::

    from src.risk.regime_throttle import get_throttle, RegimeThrottle

    t = get_throttle(regime, session)
    adjusted_score = raw_score * t.score_mult
    capped_qty = max(1, int(base_qty * t.cap_mult))
    if open_positions >= t.max_pos:
        reject(...)

All thresholds are env-overridable via ``TL_RT_*`` prefix.
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Tuple

_log = logging.getLogger("trade_labs.regime_throttle")


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return float(raw)
    except (ValueError, TypeError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return int(raw)
    except (ValueError, TypeError):
        return default


# ── Throttle configuration per regime × session ─────────────────────

@dataclass(frozen=True)
class RegimeThrottle:
    """Continuous multipliers for a given regime × session state."""
    regime: str
    session: str
    score_mult: float       # multiply unified_score by this
    cap_mult: float         # multiply position size by this
    max_pos: int            # max simultaneous positions allowed
    probe_allowed: bool     # whether MIN_PROBE trades are permitted
    probe_max_per_hour: int # max probe trades per hour


# Regime defaults (session=RTH).  Env-overridable.
_REGIME_DEFAULTS: Dict[str, Tuple[float, float, int, bool, int]] = {
    # regime:      (score_mult, cap_mult, max_pos, probe_allowed, probe/hr)
    "TREND_UP":    (1.0,  1.0,  5, True,  3),
    "TREND_DOWN":  (0.7,  0.6,  3, True,  3),
    "CHOP":        (0.4,  0.3,  1, True,  2),
    "PANIC":       (0.2,  0.15, 1, True,  1),
    "HALT":        (0.0,  0.0,  0, False, 0),
}

# Session multipliers applied on top of regime defaults
_SESSION_MULTS: Dict[str, Tuple[float, float, int]] = {
    # session:       (score_mult, cap_mult, max_pos_override_or_0)
    "REGULAR":       (1.0,  1.0,  0),  # no adjustment
    "PREMARKET":     (0.7,  0.5,  2),
    "AFTERHOURS":    (0.4,  0.3,  1),
    "OFF_HOURS":     (0.2,  0.1,  1),
}


def get_throttle(regime: str, session: str = "REGULAR") -> RegimeThrottle:
    """Return the throttle config for a given regime × session.

    Result is the regime defaults with session modifiers applied.
    All values are env-overridable: ``TL_RT_{REGIME}_{FIELD}``.
    """
    r_def = _REGIME_DEFAULTS.get(regime, _REGIME_DEFAULTS["CHOP"])
    r_score, r_cap, r_max, r_probe, r_probe_hr = r_def

    # Override per env
    r_score = _env_float(f"TL_RT_{regime}_SCORE_MULT", r_score)
    r_cap = _env_float(f"TL_RT_{regime}_CAP_MULT", r_cap)
    r_max = _env_int(f"TL_RT_{regime}_MAX_POS", r_max)
    r_probe_hr = _env_int(f"TL_RT_{regime}_PROBE_HR", r_probe_hr)

    # Apply session modifiers
    s_def = _SESSION_MULTS.get(session, _SESSION_MULTS["REGULAR"])
    s_score, s_cap, s_max_override = s_def

    final_score = r_score * s_score
    final_cap = r_cap * s_cap
    final_max = min(r_max, s_max_override) if s_max_override > 0 else r_max

    return RegimeThrottle(
        regime=regime,
        session=session,
        score_mult=round(final_score, 4),
        cap_mult=round(final_cap, 4),
        max_pos=max(0, final_max),
        probe_allowed=r_probe and (regime != "HALT"),
        probe_max_per_hour=r_probe_hr,
    )


# ── Probing budget tracker ──────────────────────────────────────────

class _ProbeBudget:
    """Tracks how many probe trades have been allowed per hour."""

    def __init__(self):
        self._hour_counts: Dict[int, int] = defaultdict(int)

    def can_probe(self, throttle: RegimeThrottle) -> bool:
        """Return True if a probe trade is allowed under the throttle."""
        if not throttle.probe_allowed:
            return False
        hour_key = int(time.time() // 3600)
        return self._hour_counts[hour_key] < throttle.probe_max_per_hour

    def record_probe(self) -> None:
        """Record that a probe trade was permitted."""
        hour_key = int(time.time() // 3600)
        self._hour_counts[hour_key] += 1
        # Evict stale hours (keep last 4)
        keys = sorted(self._hour_counts.keys())
        while len(keys) > 4:
            del self._hour_counts[keys.pop(0)]

    def probes_this_hour(self) -> int:
        hour_key = int(time.time() // 3600)
        return self._hour_counts.get(hour_key, 0)


# Module-level singleton
probe_budget = _ProbeBudget()


# ── Sizing mode selector ────────────────────────────────────────────

def select_intent_mode(
    unified_score: float,
    throttle: RegimeThrottle,
    *,
    score_full_threshold: float = 0.0,
    score_reduced_threshold: float = 0.0,
) -> str:
    """Choose FULL / REDUCED / MIN_PROBE based on throttled score.

    Parameters
    ----------
    unified_score : float
        Raw unified score before regime adjustment.
    throttle : RegimeThrottle
        Active throttle for this regime × session.
    score_full_threshold : float
        Env-overridable via ``TL_RT_SCORE_FULL_THRESH``.
    score_reduced_threshold : float
        Env-overridable via ``TL_RT_SCORE_REDUCED_THRESH``.
    """
    full_thresh = _env_float("TL_RT_SCORE_FULL_THRESH", score_full_threshold or 20.0)
    reduced_thresh = _env_float("TL_RT_SCORE_REDUCED_THRESH", score_reduced_threshold or 12.0)

    adjusted = unified_score * throttle.score_mult

    if adjusted >= full_thresh and throttle.cap_mult >= 0.5:
        return "FULL"
    if adjusted >= reduced_thresh and throttle.cap_mult >= 0.2:
        return "REDUCED"
    if throttle.probe_allowed and probe_budget.can_probe(throttle):
        return "MIN_PROBE"
    return ""  # empty string means "do not emit"


# ── Intent budget per interval ──────────────────────────────────────

@dataclass(frozen=True)
class IntentBudget:
    """Quantitative intent budget for a regime × session interval."""
    max_intents_10min: int
    max_new_names_10min: int
    risk_mult: float            # per-trade risk multiplier (FULL mode)
    probe_risk_pct: float       # probe sizing fraction (MIN_PROBE mode)
    reduced_risk_pct: float     # reduced sizing fraction (REDUCED mode)


# Budget table (regime × session) — env-overridable
# Each entry: (intents/10m, new_names/10m, risk_mult, probe_risk_pct, reduced_risk_pct)
_BUDGET_TABLE: Dict[Tuple[str, str], Tuple[int, int, float, float, float]] = {
    ("TREND_UP",   "REGULAR"):    (10, 5, 1.00, 0.05, 0.25),
    ("TREND_UP",   "PREMARKET"):  (5,  3, 0.70, 0.04, 0.20),
    ("TREND_UP",   "AFTERHOURS"): (3,  2, 0.40, 0.03, 0.15),
    ("TREND_DOWN", "REGULAR"):    (6,  3, 0.70, 0.04, 0.20),
    ("TREND_DOWN", "PREMARKET"):  (3,  2, 0.50, 0.03, 0.15),
    ("TREND_DOWN", "AFTERHOURS"): (2,  1, 0.30, 0.02, 0.10),
    ("CHOP",       "REGULAR"):    (4,  2, 0.50, 0.03, 0.15),
    ("CHOP",       "PREMARKET"):  (2,  1, 0.30, 0.02, 0.10),
    ("CHOP",       "AFTERHOURS"): (1,  1, 0.20, 0.005, 0.05),
    ("PANIC",      "REGULAR"):    (2,  1, 0.30, 0.02, 0.08),
    ("PANIC",      "PREMARKET"):  (1,  1, 0.20, 0.01, 0.05),
    ("PANIC",      "AFTERHOURS"): (1,  1, 0.10, 0.005, 0.03),
    ("HALT",       "REGULAR"):    (0,  0, 0.00, 0.00, 0.00),
}

# Fallback for unlisted combos
_BUDGET_FALLBACK = (3, 2, 0.50, 0.03, 0.15)


def get_intent_budget(regime: str, session: str) -> IntentBudget:
    """Return intent budget for regime × session with env overrides."""
    key = (regime, session)
    base = _BUDGET_TABLE.get(key, _BUDGET_FALLBACK)
    return IntentBudget(
        max_intents_10min=_env_int(f"TL_BUDGET_{regime}_{session}_INTENTS", base[0]),
        max_new_names_10min=_env_int(f"TL_BUDGET_{regime}_{session}_NAMES", base[1]),
        risk_mult=_env_float(f"TL_BUDGET_{regime}_{session}_RISK", base[2]),
        probe_risk_pct=_env_float(f"TL_BUDGET_{regime}_{session}_PROBE_PCT", base[3]),
        reduced_risk_pct=_env_float(f"TL_BUDGET_{regime}_{session}_REDUCED_PCT", base[4]),
    )


class IntentBudgetTracker:
    """Enforces per-interval intent budgets."""

    def __init__(self, interval_s: float = 600.0):
        self._interval = interval_s
        self._counts: Dict[int, int] = defaultdict(int)
        self._names: Dict[int, set] = defaultdict(set)

    def _bucket(self) -> int:
        return int(time.time() // self._interval)

    def can_emit(self, budget: IntentBudget) -> bool:
        b = self._bucket()
        return self._counts[b] < budget.max_intents_10min

    def can_emit_name(self, symbol: str, budget: IntentBudget) -> bool:
        b = self._bucket()
        if symbol in self._names.get(b, set()):
            return True  # already counting this name
        return len(self._names.get(b, set())) < budget.max_new_names_10min

    def record_emission(self, symbol: str) -> None:
        b = self._bucket()
        self._counts[b] += 1
        self._names[b].add(symbol)
        # Evict stale
        keys = sorted(self._counts.keys())
        while len(keys) > 4:
            k = keys.pop(0)
            self._counts.pop(k, None)
            self._names.pop(k, None)

    def intents_this_interval(self) -> int:
        return self._counts.get(self._bucket(), 0)

    def names_this_interval(self) -> int:
        return len(self._names.get(self._bucket(), set()))

    def budget_block_reason(self, symbol: str, budget: IntentBudget) -> str:
        """Return reason string if budget exceeded, else empty string."""
        if not self.can_emit(budget):
            return f"budget_intent_cap_{budget.max_intents_10min}"
        if not self.can_emit_name(symbol, budget):
            return f"budget_new_names_cap_{budget.max_new_names_10min}"
        return ""


# Module-level singleton
intent_budget_tracker = IntentBudgetTracker()
