"""
Dynamic Universe — real-time sector/industry leadership → active symbol set.

Assigns every symbol to one of three tiers based on current sector and
industry strength:

*  **priority** — strongest sectors + industries, scanned first
*  **active** — liquid core universe, normal scanning
*  **reduced** — cold / weak sectors, scanned infrequently

Public API
----------
``build_dynamic_universe(...)`` → DynamicUniverseDecision
``get_last_decision()``         → most recent decision (or ``None``)

Env toggles
-----------
``TL_DYNAMIC_UNIVERSE_ENABLED``     master on/off  (default ``true``)
``TL_DYN_PRIORITY_SECTOR_TOP_N``    sectors to treat as priority (default ``4``)
``TL_DYN_PRIORITY_INDUSTRY_TOP_N``  industries to treat as priority (default ``6``)
``TL_DYN_SECTOR_COLD_THRESHOLD``    score below which sector is cold (default ``30``)
``TL_DYN_INDUSTRY_COLD_THRESHOLD``  score below which industry is cold (default ``25``)
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from src.universe.sector_mapper import (
    get_all_sectors,
    get_all_industries,
    get_sector_symbols,
    get_industry_symbols,
    get_symbol_profile,
    all_symbols as _all_universe_symbols,
)
from src.monitoring.logger import get_logger

log = get_logger("dynamic_universe")

# ── Tunables ─────────────────────────────────────────────────────────

DYNAMIC_UNIVERSE_ENABLED = os.environ.get(
    "TL_DYNAMIC_UNIVERSE_ENABLED", "true"
).lower() in ("1", "true", "yes")

_PRIORITY_SECTOR_TOP_N = int(os.environ.get("TL_DYN_PRIORITY_SECTOR_TOP_N", "4"))
_PRIORITY_INDUSTRY_TOP_N = int(os.environ.get("TL_DYN_PRIORITY_INDUSTRY_TOP_N", "6"))
_SECTOR_COLD_THRESHOLD = float(os.environ.get("TL_DYN_SECTOR_COLD_THRESHOLD", "30"))
_INDUSTRY_COLD_THRESHOLD = float(os.environ.get("TL_DYN_INDUSTRY_COLD_THRESHOLD", "25"))


# ── Dataclass ────────────────────────────────────────────────────────

@dataclass(frozen=True)
class DynamicUniverseDecision:
    """Snapshot of the current dynamic universe assignment."""

    active_symbols: List[str] = field(default_factory=list)
    priority_symbols: List[str] = field(default_factory=list)
    reduced_symbols: List[str] = field(default_factory=list)
    top_sectors: List[str] = field(default_factory=list)
    top_industries: List[str] = field(default_factory=list)
    cold_sectors: List[str] = field(default_factory=list)
    cold_industries: List[str] = field(default_factory=list)
    scan_bias: Dict[str, float] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)


# ── Internal state ───────────────────────────────────────────────────

_lock = threading.Lock()
_last_decision: Optional[DynamicUniverseDecision] = None


def get_last_decision() -> Optional[DynamicUniverseDecision]:
    """Return the most recent DynamicUniverseDecision, or ``None``."""
    with _lock:
        return _last_decision


# ── Core logic ───────────────────────────────────────────────────────

def build_dynamic_universe(
    all_symbols: Optional[List[str]] = None,
    sector_scores: Optional[Dict[str, float]] = None,
    industry_scores: Optional[Dict[str, float]] = None,
    market_mode: str = "CHOP_RANGE",
) -> DynamicUniverseDecision:
    """Build the dynamic universe assignment from current scores.

    Parameters
    ----------
    all_symbols : list[str] or None
        Symbols to classify.  Defaults to the full universe CSV.
    sector_scores : dict[str, float]
        Sector name → 0-100 score.
    industry_scores : dict[str, float]
        Industry name → 0-100 score.
    market_mode : str
        Current market mode string (e.g. ``TREND_EXPANSION``).

    Returns
    -------
    DynamicUniverseDecision
    """
    global _last_decision

    if not DYNAMIC_UNIVERSE_ENABLED:
        syms = all_symbols or _all_universe_symbols()
        decision = DynamicUniverseDecision(
            active_symbols=syms,
            reasons=["dynamic_universe_disabled"],
        )
        with _lock:
            _last_decision = decision
        return decision

    syms = all_symbols or _all_universe_symbols()
    sec_scores = sector_scores or {}
    ind_scores = industry_scores or {}
    reasons: List[str] = []

    # ── Rank sectors ─────────────────────────────────────────────
    ranked_sectors: List[Tuple[str, float]] = sorted(
        ((s, sec_scores.get(s, 0.0)) for s in get_all_sectors()),
        key=lambda x: -x[1],
    )
    top_sectors = [s for s, _ in ranked_sectors[:_PRIORITY_SECTOR_TOP_N]]
    cold_sectors = [s for s, sc in ranked_sectors if sc < _SECTOR_COLD_THRESHOLD]
    reasons.append(f"top_sectors={top_sectors[:5]}")

    # ── Rank industries ──────────────────────────────────────────
    ranked_industries: List[Tuple[str, float]] = sorted(
        ((i, ind_scores.get(i, 0.0)) for i in get_all_industries()),
        key=lambda x: -x[1],
    )
    top_industries = [i for i, _ in ranked_industries[:_PRIORITY_INDUSTRY_TOP_N]]
    cold_industries = [i for i, sc in ranked_industries if sc < _INDUSTRY_COLD_THRESHOLD]
    reasons.append(f"top_industries={top_industries[:5]}")

    # ── Build sector / industry membership sets for O(1) lookup ──
    top_sector_set = set(top_sectors)
    cold_sector_set = set(cold_sectors)
    top_industry_set = set(top_industries)
    cold_industry_set = set(cold_industries)

    # ── Classify each symbol ─────────────────────────────────────
    priority: List[str] = []
    active: List[str] = []
    reduced: List[str] = []
    scan_bias: Dict[str, float] = {}

    for sym in syms:
        prof = get_symbol_profile(sym)
        sec = prof.sector
        ind = prof.industry

        # Priority: in a top sector OR a top industry
        if sec in top_sector_set or ind in top_industry_set:
            priority.append(sym)
            # Bias = average of sector + industry score, normalised
            s_sc = sec_scores.get(sec, 50.0)
            i_sc = ind_scores.get(ind, 50.0)
            scan_bias[sym] = round((s_sc + i_sc) / 2.0, 1)
            continue

        # Reduced: in a cold sector AND a cold industry
        if sec in cold_sector_set and ind in cold_industry_set:
            reduced.append(sym)
            scan_bias[sym] = round(
                (sec_scores.get(sec, 0.0) + ind_scores.get(ind, 0.0)) / 2.0, 1
            )
            continue

        # Active: everything else — liquid core
        active.append(sym)
        scan_bias[sym] = round(
            (sec_scores.get(sec, 50.0) + ind_scores.get(ind, 50.0)) / 2.0, 1
        )

    # Market mode adjustments
    if market_mode in ("DEFENSIVE_RISK_OFF", "VOLATILITY_SHOCK"):
        # Shrink priority pool in defensive regimes
        overflow = priority[_PRIORITY_SECTOR_TOP_N * 5:]
        active.extend(overflow)
        priority = priority[:_PRIORITY_SECTOR_TOP_N * 5]
        reasons.append(f"defensive_shrink mode={market_mode}")

    reasons.append(
        f"counts priority={len(priority)} active={len(active)} reduced={len(reduced)}"
    )

    decision = DynamicUniverseDecision(
        active_symbols=active,
        priority_symbols=priority,
        reduced_symbols=reduced,
        top_sectors=top_sectors,
        top_industries=top_industries,
        cold_sectors=cold_sectors,
        cold_industries=cold_industries,
        scan_bias=scan_bias,
        reasons=reasons,
    )

    with _lock:
        _last_decision = decision

    return decision
