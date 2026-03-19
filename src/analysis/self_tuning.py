"""Self-Tuning Engine — bounded adaptive parameter adjustment.

Consumes attribution stats from ``pnl_attribution`` and produces
conservative, bounded nudges for allocation weights, playbook priorities,
event-score thresholds, mode cap multipliers, and risk qty multipliers.

Safety rules
------------
- Minimum sample size before any nudge is applied.
- All deltas capped tightly (± max_delta).
- Exponential decay smoothing so values change slowly.
- Soft overrides only — never rewrites hard config.
- ``reset_tuning()`` reverts all nudges to zero instantly.

All thresholds are overridable via ``TL_TUNING_*`` env vars.
**Stocks only — no options logic.**
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

log = get_logger("self_tuning")

# ── Tunables ────────────────────────────────────────────────────────

TUNING_ENABLED: bool = os.environ.get(
    "TL_TUNING_ENABLED", "true"
).lower() in ("1", "true", "yes")

MONITOR_ENABLED: bool = os.environ.get(
    "TL_TUNING_MONITOR_ENABLED", "true"
).lower() in ("1", "true", "yes")

# Minimum closed trades per bucket before any nudge fires
_MIN_SAMPLE: int = int(os.environ.get("TL_TUNING_MIN_SAMPLE", "8"))

# Decay factor (0 → 1): higher = slower change.  0.85 = 15% new per cycle.
_DECAY: float = float(os.environ.get("TL_TUNING_DECAY", "0.85"))

# Maximum absolute deltas
_MAX_WEIGHT_DELTA: float = float(os.environ.get("TL_TUNING_MAX_WEIGHT_DELTA", "0.10"))
_MAX_THRESHOLD_DELTA: float = float(os.environ.get("TL_TUNING_MAX_THRESHOLD_DELTA", "10"))
_MAX_PRIORITY_DELTA: float = float(os.environ.get("TL_TUNING_MAX_PRIORITY_DELTA", "8.0"))
_MAX_CAP_MULT_DELTA: float = float(os.environ.get("TL_TUNING_MAX_CAP_MULT_DELTA", "0.15"))
_MAX_QTY_MULT_DELTA: float = float(os.environ.get("TL_TUNING_MAX_QTY_MULT_DELTA", "0.15"))

# Expectancy thresholds for "strong" / "weak"
_EXPECT_STRONG: float = float(os.environ.get("TL_TUNING_EXPECT_STRONG", "5.0"))    # $5+ avg
_EXPECT_WEAK: float = float(os.environ.get("TL_TUNING_EXPECT_WEAK", "-3.0"))       # -$3 avg

# Force-path for testing
_FORCE_BUCKET: str = os.environ.get("TL_TUNING_FORCE_BUCKET", "").strip().lower()
_FORCE_EDGE: str = os.environ.get("TL_TUNING_FORCE_EDGE", "").strip().lower()  # positive|negative
_FORCE_SAMPLE: int = int(os.environ.get("TL_TUNING_FORCE_SAMPLE", "0"))
_FORCE_WEIGHT_DELTA: float = float(os.environ.get("TL_TUNING_FORCE_WEIGHT_DELTA", "0"))
_FORCE_THRESHOLD_DELTA: float = float(os.environ.get("TL_TUNING_FORCE_THRESHOLD_DELTA", "0"))


# ── Data structures ─────────────────────────────────────────────────

@dataclass
class TuningDecision:
    """A single tuning nudge for one dimension."""
    dimension: str = ""         # bucket_weight / priority_bias / event_threshold / cap_mult / qty_mult
    key: str = ""               # news / rotation / volatility / meanrevert / mode name
    current_value: float = 0.0
    nudge: float = 0.0          # delta to apply
    adjusted_value: float = 0.0
    sample_count: int = 0
    expectancy: float = 0.0
    confidence: float = 0.0     # 0-1 how confident we are in nudge
    reason: str = ""


@dataclass
class TuningSnapshot:
    """Full snapshot of all active tuning state."""
    ts: float = 0.0
    enabled: bool = True
    total_decisions: int = 0
    active_overrides: int = 0
    bucket_nudges: Dict[str, float] = field(default_factory=dict)
    priority_nudges: Dict[str, float] = field(default_factory=dict)
    threshold_nudges: Dict[str, float] = field(default_factory=dict)
    cap_mult_nudges: Dict[str, float] = field(default_factory=dict)
    qty_mult_nudges: Dict[str, float] = field(default_factory=dict)
    decisions: List[TuningDecision] = field(default_factory=list)


# ── Module state (all nudges start at 0.0 = no change) ─────────────

_bucket_nudges: Dict[str, float] = {}       # bucket → weight delta
_priority_nudges: Dict[str, float] = {}     # bucket → priority bias delta
_threshold_nudges: Dict[str, float] = {}    # bucket → event-score threshold delta
_cap_mult_nudges: Dict[str, float] = {}     # mode → cap multiplier delta
_qty_mult_nudges: Dict[str, float] = {}     # bucket → qty multiplier delta
_last_compute_ts: float = 0.0
_total_decisions: int = 0

_BUCKETS = ("news", "rotation", "volatility", "meanrevert")
_MODES = ("TREND_EXPANSION", "ROTATION_TAPE", "VOLATILITY_SHOCK", "CHOP_RANGE", "DEFENSIVE_RISK_OFF")


# ── Core computation ────────────────────────────────────────────────

def _clamp(val: float, lo: float, hi: float) -> float:
    return max(lo, min(val, hi))


def _smooth(old: float, new: float, decay: float) -> float:
    """Exponential moving average: blends old value toward new."""
    return round(old * decay + new * (1.0 - decay), 6)


def _compute_bucket_nudge(bucket: str, expectancy: float, sample: int) -> float:
    """Compute a bounded weight nudge for a bucket based on expectancy."""
    if sample < _MIN_SAMPLE:
        return 0.0
    if expectancy >= _EXPECT_STRONG:
        raw = min(expectancy / 50.0, _MAX_WEIGHT_DELTA)  # scale gently
    elif expectancy <= _EXPECT_WEAK:
        raw = max(expectancy / 50.0, -_MAX_WEIGHT_DELTA)
    else:
        raw = 0.0
    return _clamp(raw, -_MAX_WEIGHT_DELTA, _MAX_WEIGHT_DELTA)


def _compute_priority_nudge(bucket: str, expectancy: float, sample: int) -> float:
    """Compute a bounded priority bias nudge."""
    if sample < _MIN_SAMPLE:
        return 0.0
    if expectancy >= _EXPECT_STRONG:
        raw = min(expectancy / 2.0, _MAX_PRIORITY_DELTA)
    elif expectancy <= _EXPECT_WEAK:
        raw = max(expectancy / 2.0, -_MAX_PRIORITY_DELTA)
    else:
        raw = 0.0
    return _clamp(raw, -_MAX_PRIORITY_DELTA, _MAX_PRIORITY_DELTA)


def _compute_threshold_nudge(bucket: str, expectancy: float, sample: int) -> float:
    """Compute event-score threshold nudge.

    Weak bukect → raise threshold (be more selective).
    Strong bucket → lower threshold (be more permissive).
    """
    if sample < _MIN_SAMPLE:
        return 0.0
    if expectancy <= _EXPECT_WEAK:
        raw = min(abs(expectancy), _MAX_THRESHOLD_DELTA)  # raise threshold
    elif expectancy >= _EXPECT_STRONG:
        raw = -min(expectancy / 2.0, _MAX_THRESHOLD_DELTA)  # lower threshold
    else:
        raw = 0.0
    return _clamp(raw, -_MAX_THRESHOLD_DELTA, _MAX_THRESHOLD_DELTA)


def _compute_cap_mult_nudge(mode: str, expectancy: float, sample: int) -> float:
    """Compute mode cap multiplier nudge."""
    if sample < _MIN_SAMPLE:
        return 0.0
    if expectancy >= _EXPECT_STRONG:
        raw = min(expectancy / 100.0, _MAX_CAP_MULT_DELTA)
    elif expectancy <= _EXPECT_WEAK:
        raw = max(expectancy / 100.0, -_MAX_CAP_MULT_DELTA)
    else:
        raw = 0.0
    return _clamp(raw, -_MAX_CAP_MULT_DELTA, _MAX_CAP_MULT_DELTA)


def _compute_qty_mult_nudge(bucket: str, expectancy: float, sample: int) -> float:
    """Compute qty multiplier nudge for risk."""
    if sample < _MIN_SAMPLE:
        return 0.0
    if expectancy >= _EXPECT_STRONG:
        raw = min(expectancy / 100.0, _MAX_QTY_MULT_DELTA)
    elif expectancy <= _EXPECT_WEAK:
        raw = max(expectancy / 100.0, -_MAX_QTY_MULT_DELTA)
    else:
        raw = 0.0
    return _clamp(raw, -_MAX_QTY_MULT_DELTA, _MAX_QTY_MULT_DELTA)


def compute_tuning_decision() -> List[TuningDecision]:
    """Compute all tuning decisions based on current attribution data.

    Pulls expectancy from ``pnl_attribution`` (lazy import to avoid
    circular dependencies), applies force-path overrides when set,
    and smooths all nudges via exponential decay.

    Returns a list of individual TuningDecision objects (one per nudge).
    """
    global _last_compute_ts, _total_decisions

    if not TUNING_ENABLED:
        return []

    from src.analysis.pnl_attribution import (
        get_bucket_expectancy,
        get_mode_expectancy,
        get_playbook_mode_expectancy,
    )

    decisions: List[TuningDecision] = []
    now = time.time()
    _last_compute_ts = now

    # ── Bucket weight + priority + threshold nudges ──────────────
    for bucket in _BUCKETS:
        # Force-path override
        if _FORCE_BUCKET and _FORCE_BUCKET == bucket and _FORCE_SAMPLE > 0:
            if _FORCE_EDGE == "positive":
                expect = abs(_FORCE_WEIGHT_DELTA) * 100 + _EXPECT_STRONG
            elif _FORCE_EDGE == "negative":
                expect = -(abs(_FORCE_WEIGHT_DELTA) * 100 + abs(_EXPECT_WEAK))
            else:
                expect = _FORCE_WEIGHT_DELTA * 50
            sample = _FORCE_SAMPLE
        else:
            expect, sample = get_bucket_expectancy(bucket)

        # Weight nudge
        raw_w = _compute_bucket_nudge(bucket, expect, sample)
        old_w = _bucket_nudges.get(bucket, 0.0)
        new_w = _smooth(old_w, raw_w, _DECAY)
        _bucket_nudges[bucket] = new_w
        confidence = min(sample / (_MIN_SAMPLE * 2), 1.0) if sample >= _MIN_SAMPLE else 0.0
        if new_w != 0.0:
            decisions.append(TuningDecision(
                dimension="bucket_weight",
                key=bucket,
                current_value=old_w,
                nudge=new_w,
                adjusted_value=new_w,
                sample_count=sample,
                expectancy=expect,
                confidence=confidence,
                reason=f"expect={expect:.2f} sample={sample}",
            ))

        # Priority nudge
        raw_p = _compute_priority_nudge(bucket, expect, sample)
        old_p = _priority_nudges.get(bucket, 0.0)
        new_p = _smooth(old_p, raw_p, _DECAY)
        _priority_nudges[bucket] = new_p
        if new_p != 0.0:
            decisions.append(TuningDecision(
                dimension="priority_bias",
                key=bucket,
                current_value=old_p,
                nudge=new_p,
                adjusted_value=new_p,
                sample_count=sample,
                expectancy=expect,
                confidence=confidence,
                reason=f"expect={expect:.2f} sample={sample}",
            ))

        # Threshold nudge
        raw_t = _compute_threshold_nudge(bucket, expect, sample)
        old_t = _threshold_nudges.get(bucket, 0.0)
        new_t = _smooth(old_t, raw_t, _DECAY)
        _threshold_nudges[bucket] = new_t
        if new_t != 0.0:
            decisions.append(TuningDecision(
                dimension="event_threshold",
                key=bucket,
                current_value=old_t,
                nudge=new_t,
                adjusted_value=new_t,
                sample_count=sample,
                expectancy=expect,
                confidence=confidence,
                reason=f"expect={expect:.2f} sample={sample}",
            ))

        # Qty multiplier nudge
        raw_q = _compute_qty_mult_nudge(bucket, expect, sample)
        old_q = _qty_mult_nudges.get(bucket, 0.0)
        new_q = _smooth(old_q, raw_q, _DECAY)
        _qty_mult_nudges[bucket] = new_q
        if new_q != 0.0:
            decisions.append(TuningDecision(
                dimension="qty_mult",
                key=bucket,
                current_value=old_q,
                nudge=new_q,
                adjusted_value=1.0 + new_q,
                sample_count=sample,
                expectancy=expect,
                confidence=confidence,
                reason=f"expect={expect:.2f} sample={sample}",
            ))

    # ── Mode cap multiplier nudges ───────────────────────────────
    for mode in _MODES:
        expect, sample = get_mode_expectancy(mode)
        # Force-path for mode
        if _FORCE_BUCKET and _FORCE_EDGE and mode == "TREND_EXPANSION":
            # Force path applies to first mode for observability
            if _FORCE_SAMPLE > 0:
                if _FORCE_EDGE == "positive":
                    expect = _EXPECT_STRONG + 5
                elif _FORCE_EDGE == "negative":
                    expect = _EXPECT_WEAK - 5
                sample = _FORCE_SAMPLE

        raw_c = _compute_cap_mult_nudge(mode, expect, sample)
        old_c = _cap_mult_nudges.get(mode, 0.0)
        new_c = _smooth(old_c, raw_c, _DECAY)
        _cap_mult_nudges[mode] = new_c
        if new_c != 0.0:
            confidence = min(sample / (_MIN_SAMPLE * 2), 1.0) if sample >= _MIN_SAMPLE else 0.0
            decisions.append(TuningDecision(
                dimension="cap_mult",
                key=mode,
                current_value=old_c,
                nudge=new_c,
                adjusted_value=1.0 + new_c,
                sample_count=sample,
                expectancy=expect,
                confidence=confidence,
                reason=f"mode_expect={expect:.2f} sample={sample}",
            ))

    _total_decisions = len(decisions)

    for d in decisions:
        log.info(
            "tuning_decision dim=%s key=%s nudge=%+.4f adjusted=%.4f "
            "sample=%d expect=%.2f conf=%.2f reason=%s",
            d.dimension, d.key, d.nudge, d.adjusted_value,
            d.sample_count, d.expectancy, d.confidence, d.reason,
        )

    return decisions


# ── Public query API (used by signal/risk arms) ─────────────────────

def get_bucket_weight_nudge(bucket: str) -> float:
    """Return current weight nudge for a bucket (can be +/-)."""
    if not TUNING_ENABLED:
        return 0.0
    return _bucket_nudges.get(bucket, 0.0)


def get_priority_nudge(bucket: str) -> float:
    """Return current priority bias nudge for a bucket."""
    if not TUNING_ENABLED:
        return 0.0
    return _priority_nudges.get(bucket, 0.0)


def get_threshold_nudge(bucket: str) -> float:
    """Return current event-score threshold nudge for a bucket."""
    if not TUNING_ENABLED:
        return 0.0
    return _threshold_nudges.get(bucket, 0.0)


def get_cap_mult_nudge(mode: str) -> float:
    """Return cap multiplier nudge for a market mode."""
    if not TUNING_ENABLED:
        return 0.0
    return _cap_mult_nudges.get(mode, 0.0)


def get_qty_mult_nudge(bucket: str) -> float:
    """Return qty multiplier nudge for a bucket (risk arm)."""
    if not TUNING_ENABLED:
        return 0.0
    return _qty_mult_nudges.get(bucket, 0.0)


def get_live_knob_overrides() -> Dict[str, Dict[str, float]]:
    """Return all non-zero nudges as a dict of dicts."""
    out: Dict[str, Dict[str, float]] = {}
    for k, v in _bucket_nudges.items():
        if v != 0.0:
            out.setdefault("bucket_weight", {})[k] = v
    for k, v in _priority_nudges.items():
        if v != 0.0:
            out.setdefault("priority_bias", {})[k] = v
    for k, v in _threshold_nudges.items():
        if v != 0.0:
            out.setdefault("event_threshold", {})[k] = v
    for k, v in _cap_mult_nudges.items():
        if v != 0.0:
            out.setdefault("cap_mult", {})[k] = v
    for k, v in _qty_mult_nudges.items():
        if v != 0.0:
            out.setdefault("qty_mult", {})[k] = v
    return out


def get_tuning_snapshot() -> TuningSnapshot:
    """Return full tuning snapshot for monitoring."""
    overrides = get_live_knob_overrides()
    active = sum(len(v) for v in overrides.values())
    return TuningSnapshot(
        ts=time.time(),
        enabled=TUNING_ENABLED,
        total_decisions=_total_decisions,
        active_overrides=active,
        bucket_nudges=dict(_bucket_nudges),
        priority_nudges=dict(_priority_nudges),
        threshold_nudges=dict(_threshold_nudges),
        cap_mult_nudges=dict(_cap_mult_nudges),
        qty_mult_nudges=dict(_qty_mult_nudges),
    )


def reset_tuning() -> None:
    """Reset all nudges to zero (instant revert)."""
    global _total_decisions
    _bucket_nudges.clear()
    _priority_nudges.clear()
    _threshold_nudges.clear()
    _cap_mult_nudges.clear()
    _qty_mult_nudges.clear()
    _total_decisions = 0
    log.info("tuning_reset all nudges cleared")
