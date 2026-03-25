"""
risk/session_gate.py

Time-of-day trade gating with quality scores.
Called by risk arm before approving any trade.
Quality score feeds into position_sizing.calculate_position_size().
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


@dataclass(frozen=True)
class GateResult:
    allowed: bool
    reason: str
    quality_score: float        # 0.0 = blocked, 1.0 = full size


# (start, end, quality, label)
_WINDOWS = [
    (time(9, 30),  time(9, 46),  0.0,  "open_chaos_block"),
    (time(9, 46),  time(10, 30), 0.75, "early_momentum"),
    (time(10, 30), time(11, 45), 1.0,  "prime_window"),
    (time(11, 45), time(13, 30), 0.55, "midday_drift"),
    (time(13, 30), time(15, 30), 0.85, "afternoon_momentum"),
    (time(15, 30), time(15, 50), 0.60, "late_day"),
    (time(15, 50), time(16, 0),  0.0,  "close_chaos_block"),
]


def check_session_gate(now: datetime | None = None) -> GateResult:
    """
    Returns GateResult for current time (ET).
    Pass `now` explicitly for testing; omit for live use.
    """
    if now is None:
        now = datetime.now(ET)

    current_time = now.astimezone(ET).time()

    for start, end, quality, label in _WINDOWS:
        if start <= current_time < end:
            return GateResult(
                allowed=quality > 0.0,
                reason=label,
                quality_score=quality,
            )

    return GateResult(
        allowed=False,
        reason="outside_market_hours",
        quality_score=0.0,
    )
