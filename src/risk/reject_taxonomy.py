"""Risk rejection taxonomy and throughput telemetry.

Provides machine-stable rejection reason codes, per-reason counters,
and a periodic ``RISKTHRU`` heartbeat line for at-a-glance diagnostics.

Usage::

    from src.risk.reject_taxonomy import RiskRejectReason, risk_telemetry

    risk_telemetry.record_reject(RiskRejectReason.SESSION_RULE)
    risk_telemetry.record_approve()
    risk_telemetry.log_heartbeat()
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from enum import Enum

_log = logging.getLogger("trade_labs.risk_telemetry")


class RiskRejectReason(str, Enum):
    """Machine-stable risk rejection codes.

    Mirrors and extends PlanReject with more granularity for
    session/broker/budget-level rejections.
    """
    # Micro-risk constraints
    RISK_BUDGET = "risk_budget"           # per-trade $ cap exceeded
    SYMBOL_BAN = "symbol_ban"             # banned / illiquid symbol
    DD_LIMIT = "dd_limit"                 # daily drawdown limit hit
    VOL_SPIKE = "vol_spike"               # volatility spike guard
    SPREAD_BAD = "spread_bad"             # spread too wide
    STOP_INVALID = "stop_invalid"         # invalid stop / entry zone
    SIZE_ZERO = "size_zero"               # sizing yields 0 shares
    R_MULT_LOW = "r_mult_low"             # reward:risk too low
    NOTIONAL_SMALL = "notional_small"     # notional below floor

    # High-level session / posture
    SESSION_RULE = "session_rule"         # time-of-day block
    REGIME_THROTTLE = "regime_throttle"   # regime multiplier killed qty
    BUDGET_BLOCK = "budget_block"         # intent budget exhausted

    # Portfolio-level
    MAX_POSITIONS = "max_positions"       # at position cap
    PORTFOLIO_HEAT = "portfolio_heat"     # total heat exceeded
    SECTOR_LIMIT = "sector_limit"         # sector concentration
    ALLOCATION_FULL = "allocation_full"   # allocation bucket full
    DUPLICATE_SYMBOL = "duplicate_symbol" # already in this name

    # Infrastructure
    BROKER_LIMIT = "broker_limit"         # broker connection / margin
    CIRCUIT_BREAKER = "circuit_breaker"   # kill switch active
    RISK_GUARD = "risk_guard"             # generic risk guard
    SIZING_ERROR = "sizing_error"         # computation error

    OTHER = "other"


class RiskTelemetry:
    """Per-process risk throughput counters and heartbeat logger."""

    def __init__(self):
        self._intents_seen = 0
        self._approved = 0
        self._rejected = 0
        self._reject_reasons: Counter = Counter()
        self._shadow_approvals = 0  # would-have-passed under +20% cap
        self._last_heartbeat_ts = 0.0

    def record_approve(self) -> None:
        self._intents_seen += 1
        self._approved += 1

    def record_reject(self, reason: RiskRejectReason) -> None:
        self._intents_seen += 1
        self._rejected += 1
        self._reject_reasons[reason.value] += 1

    def record_shadow_approval(self) -> None:
        """Count an intent that would have passed with +20% looser limits."""
        self._shadow_approvals += 1

    def log_heartbeat(self) -> None:
        """Log the RISKTHRU heartbeat line."""
        top = dict(self._reject_reasons.most_common(3))
        top_str = ",".join(f"{k}={v}" for k, v in top.items()) if top else "none"
        _log.info(
            "RISKTHRU intents=%d approved=%d rejected=%d "
            "top_reject=%s shadow_approvals=%d",
            self._intents_seen,
            self._approved,
            self._rejected,
            top_str,
            self._shadow_approvals,
        )
        self._last_heartbeat_ts = time.time()

    def get_summary(self) -> dict:
        return {
            "intents_seen": self._intents_seen,
            "approved": self._approved,
            "rejected": self._rejected,
            "reject_breakdown": dict(self._reject_reasons.most_common(20)),
            "shadow_approvals": self._shadow_approvals,
        }

    def log_whatif_report(self, looseness_pct: float = 20.0) -> None:
        """Log a what-if summary at end of run or on demand.

        Example output::

            RISK_WHATIF Under current settings: 2 approved, 8 rejected, \
            5 shadow-approved at +20% capmult. Top rejects: session_rule=3, \
            regime_throttle=2. Loosening could yield ~62% more fills.
        """
        if self._intents_seen == 0:
            _log.info("RISK_WHATIF No intents processed — nothing to report.")
            return

        top = self._reject_reasons.most_common(5)
        top_str = ", ".join(f"{k}={v}" for k, v in top) if top else "none"

        potential_pct = 0.0
        if self._rejected > 0:
            potential_pct = (self._shadow_approvals / self._rejected) * 100

        _log.info(
            "RISK_WHATIF Under current settings: %d approved, %d rejected, "
            "%d shadow-approved at +%.0f%% capmult. "
            "Top rejects: %s. "
            "Loosening could yield ~%.0f%% more fills.",
            self._approved,
            self._rejected,
            self._shadow_approvals,
            looseness_pct,
            top_str,
            potential_pct,
        )


# Module-level singleton
risk_telemetry = RiskTelemetry()
