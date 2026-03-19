"""
Sector Rotation Selector — determine which sectors/industries are
leading, rotating-in, or rotating-out.

Uses live scores from ``sector_intel`` and ``industry_rotation`` to
produce a snapshot decision consumed by the allocation engine, the
signal arm, and the risk arm.

Public API
----------
``compute_sector_rotation_decision(...)`` → SectorRotationDecision
``get_last_rotation_decision()``          → most recent decision

Env toggles
-----------
``TL_SECTOR_ROTATION_SEL_ENABLED``     master on/off (default ``true``)
``TL_ROTATION_SEL_TOP_SECTORS``        # top sectors (default ``4``)
``TL_ROTATION_SEL_TOP_INDUSTRIES``     # top industries (default ``6``)
``TL_ROTATION_SEL_IN_THRESHOLD``       score delta for "rotating-in" (default ``10``)
``TL_ROTATION_SEL_OUT_THRESHOLD``      score below which = "rotating-out" (default ``30``)
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.monitoring.logger import get_logger

log = get_logger("sector_rotation_sel")

# ── Tunables ─────────────────────────────────────────────────────────

ROTATION_SEL_ENABLED = os.environ.get(
    "TL_SECTOR_ROTATION_SEL_ENABLED", "true"
).lower() in ("1", "true", "yes")

_TOP_SECTORS_N = int(os.environ.get("TL_ROTATION_SEL_TOP_SECTORS", "4"))
_TOP_INDUSTRIES_N = int(os.environ.get("TL_ROTATION_SEL_TOP_INDUSTRIES", "6"))
_IN_THRESHOLD = float(os.environ.get("TL_ROTATION_SEL_IN_THRESHOLD", "10"))
_OUT_THRESHOLD = float(os.environ.get("TL_ROTATION_SEL_OUT_THRESHOLD", "30"))


# ── Dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SectorRotationDecision:
    """Snapshot of current sector rotation state."""

    top_sectors: List[Tuple[str, float]] = field(default_factory=list)
    rotating_in: List[str] = field(default_factory=list)
    rotating_out: List[str] = field(default_factory=list)
    top_industries: List[Tuple[str, float]] = field(default_factory=list)
    leadership_score: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


# ── Historical tracking for trend detection ──────────────────────────

_lock = threading.Lock()
_last_decision: Optional[SectorRotationDecision] = None
_prev_sector_scores: Dict[str, float] = {}          # sector → previous score
_prev_industry_scores: Dict[str, float] = {}
_last_compute_ts: float = 0.0


def get_last_rotation_decision() -> Optional[SectorRotationDecision]:
    """Return the most recent SectorRotationDecision, or ``None``."""
    with _lock:
        return _last_decision


# ── Core logic ───────────────────────────────────────────────────────

def compute_sector_rotation_decision(
    sector_scores: Dict[str, float],
    industry_scores: Dict[str, float],
    market_mode: str = "CHOP_RANGE",
) -> SectorRotationDecision:
    """Evaluate current scores and determine rotation state.

    Parameters
    ----------
    sector_scores : dict[str, float]
        Sector → 0-100 score (from ``get_sector_score``).
    industry_scores : dict[str, float]
        Industry → 0-100 score (from ``get_industry_score``).
    market_mode : str
        Current market mode string.

    Returns
    -------
    SectorRotationDecision
    """
    global _last_decision, _prev_sector_scores, _prev_industry_scores, _last_compute_ts

    if not ROTATION_SEL_ENABLED:
        decision = SectorRotationDecision(reasons=["rotation_selector_disabled"])
        with _lock:
            _last_decision = decision
        return decision

    reasons: List[str] = []

    # ── Rank sectors ─────────────────────────────────────────────
    ranked_sectors: List[Tuple[str, float]] = sorted(
        sector_scores.items(),
        key=lambda x: -x[1],
    )
    top_sectors = ranked_sectors[:_TOP_SECTORS_N]
    reasons.append(
        f"top_sectors=[{', '.join(f'{s}:{sc:.0f}' for s, sc in top_sectors)}]"
    )

    # ── Rank industries ──────────────────────────────────────────
    ranked_industries: List[Tuple[str, float]] = sorted(
        industry_scores.items(),
        key=lambda x: -x[1],
    )
    top_industries = ranked_industries[:_TOP_INDUSTRIES_N]

    # ── Detect rotating-in / rotating-out ────────────────────────
    rotating_in: List[str] = []
    rotating_out: List[str] = []

    with _lock:
        prev_sec = dict(_prev_sector_scores)

    for sector, score in sector_scores.items():
        prev = prev_sec.get(sector, score)
        delta = score - prev

        # Rotating-in: significant improvement and above-average
        if delta >= _IN_THRESHOLD and score >= 50:
            rotating_in.append(sector)
        # Rotating-out: score is cold
        elif score < _OUT_THRESHOLD:
            rotating_out.append(sector)

    if rotating_in:
        reasons.append(f"rotating_in={rotating_in}")
    if rotating_out:
        reasons.append(f"rotating_out={rotating_out}")

    # ── Leadership score: blend of raw score + momentum ──────────
    leadership: Dict[str, float] = {}
    for sector, score in sector_scores.items():
        prev = prev_sec.get(sector, score)
        momentum = score - prev
        # Leadership = 70% level + 30% momentum (clamped)
        lead = 0.70 * score + 0.30 * max(-20.0, min(20.0, momentum)) * 2.5
        leadership[sector] = round(max(0.0, min(100.0, lead)), 1)

    # Market mode modifier
    if market_mode == "TREND_EXPANSION":
        reasons.append("mode_boost=aggressive_rotation")
    elif market_mode in ("DEFENSIVE_RISK_OFF", "VOLATILITY_SHOCK"):
        reasons.append("mode_caution=reduced_rotation")

    decision = SectorRotationDecision(
        top_sectors=top_sectors,
        rotating_in=rotating_in,
        rotating_out=rotating_out,
        top_industries=top_industries,
        leadership_score=leadership,
        reasons=reasons,
    )

    # ── Update history ───────────────────────────────────────────
    with _lock:
        _prev_sector_scores = dict(sector_scores)
        _prev_industry_scores = dict(industry_scores)
        _last_decision = decision
        _last_compute_ts = time.time()

    return decision
