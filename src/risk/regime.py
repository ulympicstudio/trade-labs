"""
risk/regime.py

Minimal regime detector used by live_loop_10s.
Safe defaults: if anything fails, return YELLOW (neutral).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RegimeResult:
    # live_loop_10s expects: regime.regime
    regime: str  # "GREEN" | "YELLOW" | "RED"
    confidence: float = 0.5
    reason: str = "default-neutral"
    # Optional metadata (won't break if unused)
    vol: Optional[float] = None
    trend: Optional[float] = None


def get_regime(*args, **kwargs) -> RegimeResult:
    """
    Minimal implementation:
    - Returns a neutral regime unless you later implement real logic.
    - Signature accepts anything so callers don't break.
    """
    return RegimeResult(regime="YELLOW", confidence=0.5, reason="placeholder")


# Backwards/alternate naming safety (in case older code expects it)
def get_market_regime(*args, **kwargs) -> RegimeResult:
    return get_regime(*args, **kwargs)