"""Intent Fate Tracker — per-intent funnel logging with machine-stable reason codes.

Every trade intent gets a structured fate log line as it moves through
signal → risk → execution.  The per-cycle conservation heartbeat proves
that intents_in == approved + rejected and approved == transmitted + exec_blocked.

Usage in each arm::

    from src.monitoring.intent_fate import fate_tracker

    fate_tracker.record_emission(intent_id, symbol, strategy)
    fate_tracker.record_risk_verdict(intent_id, symbol, approved=True)
    fate_tracker.record_risk_verdict(intent_id, symbol, approved=False, reason=ExecFate.RISK_SESSION_BLOCK)
    fate_tracker.record_exec_verdict(intent_id, symbol, ExecFate.TRANSMITTED)
    fate_tracker.log_conservation()

Reason codes are intentionally flat strings so they can be grepped / counted
without importing Python enums.
"""

from __future__ import annotations

import logging
import time
from collections import Counter
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, Optional

_log = logging.getLogger("trade_labs.intent_fate")


# ── Machine-stable reason codes ──────────────────────────────────────

class RiskFate(str, Enum):
    """Risk arm rejection reasons (superset of PlanReject for fate tracking)."""
    APPROVED = "RISK_APPROVED"
    SESSION_BLOCK = "RISK_SESSION_BLOCK"
    QUOTE_QUALITY = "RISK_QUOTE_QUALITY"
    MAX_POSITIONS = "RISK_MAX_POSITIONS"
    PORTFOLIO_HEAT = "RISK_PORTFOLIO_HEAT"
    SIZE_ZERO = "RISK_SIZE_ZERO"
    STOP_INVALID = "RISK_STOP_INVALID"
    R_MULT_TOO_LOW = "RISK_R_MULT_TOO_LOW"
    NOTIONAL_TOO_SMALL = "RISK_NOTIONAL_TOO_SMALL"
    DUPLICATE_SYMBOL = "RISK_DUPLICATE_SYMBOL"
    REGIME_BLOCK = "RISK_REGIME_BLOCK"
    RISK_CAP = "RISK_CAP_EXCEEDED"
    SECTOR_LIMIT = "RISK_SECTOR_LIMIT"
    ALLOCATION_FULL = "RISK_ALLOCATION_FULL"
    CIRCUIT_BREAKER = "RISK_CIRCUIT_BREAKER"
    RISK_GUARD = "RISK_GUARD"
    SIZING_ERROR = "RISK_SIZING_ERROR"
    OTHER = "RISK_OTHER"


class ExecFate(str, Enum):
    """Execution arm disposition codes."""
    TRANSMITTED = "EXEC_TRANSMITTED"
    PAPER_FILL = "EXEC_PAPER_FILL"
    SESSION_BLOCKED = "EXEC_SESSION_BLOCKED"
    AH_BLOCKED = "EXEC_AH_BLOCKED"
    ADAPTER_ERROR = "EXEC_ADAPTER_ERROR"
    EXECUTION_ERROR = "EXEC_EXECUTION_ERROR"
    # Blueprint dispositions
    BP_STAGED = "EXEC_BP_STAGED"
    BP_TRANSMITTED = "EXEC_BP_TRANSMITTED"
    BP_CANCELLED = "EXEC_BP_CANCELLED"
    BP_SKIPPED = "EXEC_BP_SKIPPED"
    BP_AMENDED = "EXEC_BP_AMENDED"


# ── Per-intent record ────────────────────────────────────────────────

@dataclass
class _FateRecord:
    intent_id: str
    symbol: str
    strategy: str = ""
    emitted_ts: float = 0.0
    risk_fate: str = ""        # RiskFate value or ""
    risk_ts: float = 0.0
    exec_fate: str = ""        # ExecFate value or ""
    exec_ts: float = 0.0


# ── Tracker ──────────────────────────────────────────────────────────

class FateTracker:
    """Per-process intent fate tracker with structured logging.

    Each arm calls the appropriate ``record_*`` method.  Because arms
    run as separate processes, each process maintains its own slice of
    the fate ledger.  The conservation heartbeat validates local counts.
    """

    def __init__(self, max_records: int = 5000):
        self._records: Dict[str, _FateRecord] = {}
        self._max = max_records
        # Counters for conservation
        self._emitted = 0
        self._risk_approved = 0
        self._risk_rejected = 0
        self._exec_transmitted = 0
        self._exec_blocked = 0
        self._risk_reason_counts: Counter = Counter()
        self._exec_reason_counts: Counter = Counter()

    # ── Recording methods ────────────────────────────────────────

    def record_emission(
        self, intent_id: str, symbol: str, strategy: str = ""
    ) -> None:
        """Signal arm: intent emitted onto the bus."""
        self._emitted += 1
        self._records[intent_id] = _FateRecord(
            intent_id=intent_id,
            symbol=symbol,
            strategy=strategy,
            emitted_ts=time.time(),
        )
        _log.info(
            "[FATE] emitted intent_id=%s sym=%s strategy=%s",
            intent_id, symbol, strategy,
        )
        self._maybe_evict()

    def record_risk_verdict(
        self,
        intent_id: str,
        symbol: str,
        *,
        approved: bool,
        reason: str = "",
    ) -> None:
        """Risk arm: intent approved or rejected."""
        rec = self._records.get(intent_id)
        if rec is None:
            rec = _FateRecord(intent_id=intent_id, symbol=symbol)
            self._records[intent_id] = rec

        fate_code = RiskFate.APPROVED.value if approved else (reason or RiskFate.OTHER.value)
        rec.risk_fate = fate_code
        rec.risk_ts = time.time()

        if approved:
            self._risk_approved += 1
        else:
            self._risk_rejected += 1
            self._risk_reason_counts[fate_code] += 1

        _log.info(
            "[FATE] risk_verdict intent_id=%s sym=%s approved=%s reason=%s",
            intent_id, symbol, approved, fate_code,
        )

    def record_exec_verdict(
        self,
        intent_id: str,
        symbol: str,
        fate: str,
    ) -> None:
        """Execution arm: final disposition of the intent."""
        rec = self._records.get(intent_id)
        if rec is None:
            rec = _FateRecord(intent_id=intent_id, symbol=symbol)
            self._records[intent_id] = rec

        rec.exec_fate = fate
        rec.exec_ts = time.time()
        self._exec_reason_counts[fate] += 1

        if fate in (ExecFate.TRANSMITTED.value, ExecFate.PAPER_FILL.value,
                     ExecFate.BP_TRANSMITTED.value):
            self._exec_transmitted += 1
        else:
            self._exec_blocked += 1

        _log.info(
            "[FATE] exec_verdict intent_id=%s sym=%s fate=%s",
            intent_id, symbol, fate,
        )

    # ── Conservation heartbeat ───────────────────────────────────

    def log_conservation(self) -> None:
        """Log a conservation line: emitted = approved + rejected, etc.

        Called periodically from the arm's heartbeat tick.
        """
        risk_total = self._risk_approved + self._risk_rejected
        exec_total = self._exec_transmitted + self._exec_blocked

        # Top rejection reasons (up to 5)
        top_risk = dict(self._risk_reason_counts.most_common(5))
        top_exec = dict(self._exec_reason_counts.most_common(5))

        _log.info(
            "[FATE] conservation  emitted=%d  risk_in=%d  "
            "risk_approved=%d  risk_rejected=%d  "
            "exec_transmitted=%d  exec_blocked=%d  "
            "top_risk_reasons=%s  top_exec_reasons=%s",
            self._emitted,
            risk_total,
            self._risk_approved,
            self._risk_rejected,
            self._exec_transmitted,
            self._exec_blocked,
            top_risk,
            top_exec,
        )

    # ── Diagnostics ──────────────────────────────────────────────

    def get_summary(self) -> dict:
        """Return a dict summary for dashboard/diagnostics."""
        return {
            "emitted": self._emitted,
            "risk_approved": self._risk_approved,
            "risk_rejected": self._risk_rejected,
            "exec_transmitted": self._exec_transmitted,
            "exec_blocked": self._exec_blocked,
            "top_risk_reasons": dict(self._risk_reason_counts.most_common(10)),
            "top_exec_reasons": dict(self._exec_reason_counts.most_common(10)),
            "tracked_intents": len(self._records),
        }

    def _maybe_evict(self) -> None:
        """Keep the record dict bounded by evicting oldest entries."""
        if len(self._records) > self._max:
            excess = len(self._records) - self._max
            keys = list(self._records.keys())[:excess]
            for k in keys:
                del self._records[k]


# Module-level singleton — one per process.
fate_tracker = FateTracker()
