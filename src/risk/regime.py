"""
risk/regime.py

Minimal regime detector used by live_loop_10s.
Safe defaults: if anything fails, return YELLOW (neutral).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional, List


@dataclass(frozen=True)
class RegimeResult:
    # live_loop_10s expects these attributes:
    regime: str  # "GREEN" | "YELLOW" | "RED"
    reasons: List[str] = field(default_factory=list)

    # extra helpful fields (safe if unused)
    confidence: float = 0.5
    reason: str = "default-neutral"  # keep singular too (human-readable)
    vol: Optional[float] = None
    trend: Optional[float] = None


def get_regime(*args, **kwargs) -> RegimeResult:
    """
    Minimal implementation:
    - Returns a neutral regime unless you later implement real logic.
    - Signature accepts anything so callers don't break.
    """
    return RegimeResult(
        regime="YELLOW",
        confidence=0.5,
        reason="placeholder",
        reasons=["placeholder"],
    )


# Backwards/alternate naming safety
def get_market_regime(*args, **kwargs) -> RegimeResult:
    return get_regime(*args, **kwargs)