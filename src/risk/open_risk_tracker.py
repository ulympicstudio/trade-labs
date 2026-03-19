"""Track aggregate open risk (sum of risk_usd across active positions).

Used by :mod:`risk_guard` / :mod:`risk_main` to enforce the 2 %
portfolio-level risk cap.  Module-level state — same pattern as
:mod:`kill_switch`.
"""

from __future__ import annotations

import logging
from typing import Dict

log = logging.getLogger(__name__)

# symbol → risk_usd for every position the risk arm has approved
_open_positions: Dict[str, float] = {}


def record_fill(symbol: str, risk_usd: float) -> None:
    """Register (or update) the risk for an approved trade."""
    prev = _open_positions.get(symbol)
    _open_positions[symbol] = risk_usd
    if prev is not None:
        log.info(
            "open_risk_update symbol=%s risk_usd=%.2f (was %.2f) total=%.2f positions=%d",
            symbol, risk_usd, prev, get_total_open_risk(), len(_open_positions),
        )
    else:
        log.info(
            "open_risk_add symbol=%s risk_usd=%.2f total=%.2f positions=%d",
            symbol, risk_usd, get_total_open_risk(), len(_open_positions),
        )


def record_close(symbol: str) -> None:
    """Remove a symbol from tracking (no error if missing)."""
    removed = _open_positions.pop(symbol, None)
    if removed is not None:
        log.info(
            "open_risk_close symbol=%s freed=%.2f total=%.2f positions=%d",
            symbol, removed, get_total_open_risk(), len(_open_positions),
        )


def get_total_open_risk() -> float:
    """Sum of risk_usd across all tracked positions."""
    return round(sum(_open_positions.values()), 2)


def get_position_count() -> int:
    return len(_open_positions)


def get_all_positions() -> Dict[str, float]:
    """Return a shallow copy of the positions dict."""
    return dict(_open_positions)


def reset() -> None:
    """Clear all tracked positions (call at session start)."""
    count = len(_open_positions)
    _open_positions.clear()
    log.info("open_risk_reset cleared=%d", count)
