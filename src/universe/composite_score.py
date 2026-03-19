"""
Composite Score — Market → Sector → Industry → Symbol hierarchy scorer.

Combines four levels of intelligence into a single composite_score
that augments the existing signal scoring pipeline.

Formula::

    composite_score = 0.15 * market_score
                    + 0.25 * sector_score
                    + 0.20 * industry_score
                    + 0.40 * symbol_score

All component scores are on a 0–100 scale.

Public API
----------
``compute_composite(symbol, symbol_score, market_mode_decision)``
    → CompositeResult dataclass

``get_market_score(market_mode_decision)``
    → float 0-100

Env toggles
-----------
``TL_COMPOSITE_ENABLED``     master on/off (default ``true``)
``TL_COMPOSITE_W_MARKET``    market weight (default ``0.15``)
``TL_COMPOSITE_W_SECTOR``    sector weight (default ``0.25``)
``TL_COMPOSITE_W_INDUSTRY``  industry weight (default ``0.20``)
``TL_COMPOSITE_W_SYMBOL``    symbol weight (default ``0.40``)
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from src.universe.sector_mapper import get_sector, get_industry
from src.signals.sector_intel import get_sector_score
from src.signals.industry_rotation import get_industry_score
from src.monitoring.logger import get_logger

log = get_logger("composite_score")

# ── Tunables ─────────────────────────────────────────────────────────

COMPOSITE_ENABLED = os.environ.get(
    "TL_COMPOSITE_ENABLED", "true"
).lower() in ("1", "true", "yes")

_W_MARKET = float(os.environ.get("TL_COMPOSITE_W_MARKET", "0.15"))
_W_SECTOR = float(os.environ.get("TL_COMPOSITE_W_SECTOR", "0.25"))
_W_INDUSTRY = float(os.environ.get("TL_COMPOSITE_W_INDUSTRY", "0.20"))
_W_SYMBOL = float(os.environ.get("TL_COMPOSITE_W_SYMBOL", "0.40"))


@dataclass(frozen=True)
class CompositeResult:
    """Result of the four-level composite scoring."""

    symbol: str = ""
    symbol_score: float = 0.0
    sector_score: float = 0.0
    industry_score: float = 0.0
    market_score: float = 0.0
    composite_score: float = 0.0
    sector: str = ""
    industry: str = ""


# ── Market-level score from MarketModeDecision ──────────────────────

# Map mode + posture to a base score
_MODE_SCORE = {
    "TREND_EXPANSION": 80,
    "ROTATION_TAPE": 65,
    "VOLATILITY_SHOCK": 40,
    "CHOP_RANGE": 50,
    "DEFENSIVE_RISK_OFF": 25,
}

_POSTURE_BONUS = {
    "AGGRESSIVE": 10,
    "NORMAL": 0,
    "DEFENSIVE": -10,
    "MINIMAL": -20,
}


def get_market_score(market_mode_decision: object = None) -> float:
    """Derive a 0–100 market score from the MarketModeDecision.

    If no decision is available, returns 50 (neutral).
    """
    if market_mode_decision is None:
        return 50.0

    mode = getattr(market_mode_decision, "mode", "CHOP_RANGE")
    posture = getattr(market_mode_decision, "risk_posture", "NORMAL")
    confidence = getattr(market_mode_decision, "confidence", 0.5)

    base = _MODE_SCORE.get(mode, 50)
    bonus = _POSTURE_BONUS.get(posture, 0)
    # Confidence scales the base (0.0-1.0 → 60%-100% of base)
    conf_mult = 0.6 + 0.4 * min(1.0, max(0.0, confidence))
    raw = base * conf_mult + bonus
    return round(max(0.0, min(100.0, raw)), 1)


# ── Core composite computation ──────────────────────────────────────

def compute_composite(
    symbol: str,
    symbol_score: float,
    market_mode_decision: object = None,
) -> CompositeResult:
    """Compute the four-level composite score for *symbol*.

    Parameters
    ----------
    symbol : str
        Ticker symbol.
    symbol_score : float
        The existing signal-level score for this symbol (0-100).
    market_mode_decision : MarketModeDecision or None
        Current market mode decision (for market_score derivation).

    Returns
    -------
    CompositeResult with all component scores and the weighted composite.
    """
    if not COMPOSITE_ENABLED:
        return CompositeResult(
            symbol=symbol,
            symbol_score=symbol_score,
            composite_score=symbol_score,
            sector=get_sector(symbol),
            industry=get_industry(symbol),
        )

    sector = get_sector(symbol)
    industry = get_industry(symbol)

    market_sc = get_market_score(market_mode_decision)
    sector_sc = get_sector_score(sector)
    industry_sc = get_industry_score(industry)
    sym_sc = max(0.0, min(100.0, symbol_score))

    composite = (
        _W_MARKET * market_sc
        + _W_SECTOR * sector_sc
        + _W_INDUSTRY * industry_sc
        + _W_SYMBOL * sym_sc
    )

    return CompositeResult(
        symbol=symbol,
        symbol_score=round(sym_sc, 1),
        sector_score=round(sector_sc, 1),
        industry_score=round(industry_sc, 1),
        market_score=round(market_sc, 1),
        composite_score=round(composite, 1),
        sector=sector,
        industry=industry,
    )
