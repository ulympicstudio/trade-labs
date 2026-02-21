"""
regime.py

Minimal market regime detector used by live_loop_10s.

This is intentionally conservative and lightweight:
- If we cannot compute a regime reliably, we return NORMAL.
- You can later upgrade this to use SPY/QQQ trend + vol + breadth.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RegimeResult:
    name: str  # e.g., "NORMAL", "RISK_OFF", "RISK_ON"
    confidence: float  # 0.0 - 1.0
    reason: str = ""


def get_regime(*args, **kwargs) -> RegimeResult:
    """
    Placeholder regime classifier.

    live_loop_10s imports this, so we provide a stable API now.
    Later: accept ib + symbols, compute trend/volatility, etc.
    """
    return RegimeResult(
        name="NORMAL",
        confidence=0.5,
        reason="Regime module placeholder (default NORMAL)",
    )