"""Cross-arm throughput and choke-point dashboard.

A single reconciliation view across signal → risk → execution:

    candidates_seen → intents_emitted → orderplans → orders_submitted → fills

Each arm pushes its slice of the funnel into a shared data structure.
The monitor arm (or any periodic tick) calls ``render()`` to log the
full funnel, top reject reasons, and regime × time-of-day heatmap.

Usage::

    from src.monitoring.throughput_dashboard import throughput

    # Signal arm
    throughput.record_candidates_seen(n)
    throughput.record_intent_emitted(symbol, regime, session)

    # Risk arm
    throughput.record_risk_approve(symbol)
    throughput.record_risk_reject(symbol, reason)

    # Execution arm
    throughput.record_order_submitted(symbol)
    throughput.record_fill(symbol, qty, pnl)

    # Any arm (periodic)
    throughput.log_funnel()

Thread-safe: all mutations are protected by a lock.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from typing import Dict, Deque, List, Tuple

_log = logging.getLogger("trade_labs.throughput")


@dataclass
class _FunnelSnapshot:
    """Immutable snapshot of the funnel for rendering."""
    candidates_seen: int = 0
    intents_emitted: int = 0
    risk_approved: int = 0
    risk_rejected: int = 0
    orders_submitted: int = 0
    fills: int = 0
    total_pnl: float = 0.0


class ThroughputDashboard:
    """Per-process throughput tracker with funnel accounting."""

    def __init__(self):
        self._lock = threading.Lock()
        # Funnel counters
        self._candidates_seen = 0
        self._intents_emitted = 0
        self._risk_approved = 0
        self._risk_rejected = 0
        self._orders_submitted = 0
        self._fills = 0
        self._total_pnl = 0.0
        # Reject histograms
        self._signal_reject_reasons: Counter = Counter()
        self._risk_reject_reasons: Counter = Counter()
        # Regime × hour heatmap
        self._regime_hour_intents: Dict[Tuple[str, int], int] = defaultdict(int)
        self._regime_hour_approvals: Dict[Tuple[str, int], int] = defaultdict(int)
        # Rolling cycle history for monitor arm (last ~30 snapshots ≈ 5 min)
        self._cycle_history: Deque[_FunnelSnapshot] = deque(maxlen=30)

    # ── Signal arm ──────────────────────────────────────────────

    def record_candidates_seen(self, n: int = 1) -> None:
        with self._lock:
            self._candidates_seen += n

    def record_intent_emitted(
        self, symbol: str, regime: str = "", session: str = ""
    ) -> None:
        with self._lock:
            self._intents_emitted += 1
            hour = time.localtime().tm_hour
            self._regime_hour_intents[(regime, hour)] += 1

    def record_signal_reject(self, reason: str) -> None:
        with self._lock:
            self._signal_reject_reasons[reason] += 1

    # ── Risk arm ────────────────────────────────────────────────

    def record_risk_approve(self, symbol: str = "") -> None:
        with self._lock:
            self._risk_approved += 1
            hour = time.localtime().tm_hour
            # Use last known regime if available
            self._regime_hour_approvals[("", hour)] += 1

    def record_risk_reject(self, symbol: str, reason: str) -> None:
        with self._lock:
            self._risk_rejected += 1
            self._risk_reject_reasons[reason] += 1

    # ── Execution arm ───────────────────────────────────────────

    def record_order_submitted(self, symbol: str = "") -> None:
        with self._lock:
            self._orders_submitted += 1

    def record_fill(self, symbol: str, qty: int = 0, pnl: float = 0.0) -> None:
        with self._lock:
            self._fills += 1
            self._total_pnl += pnl

    # ── Rendering ───────────────────────────────────────────────

    def snapshot(self) -> _FunnelSnapshot:
        with self._lock:
            return _FunnelSnapshot(
                candidates_seen=self._candidates_seen,
                intents_emitted=self._intents_emitted,
                risk_approved=self._risk_approved,
                risk_rejected=self._risk_rejected,
                orders_submitted=self._orders_submitted,
                fills=self._fills,
                total_pnl=self._total_pnl,
            )

    def log_funnel(self) -> None:
        """Log the full cycle-level funnel accounting line."""
        s = self.snapshot()
        risk_total = s.risk_approved + s.risk_rejected
        _log.info(
            "FUNNEL candidates=%d intents=%d risk_in=%d "
            "approved=%d rejected=%d orders=%d fills=%d pnl=$%.2f",
            s.candidates_seen,
            s.intents_emitted,
            risk_total,
            s.risk_approved,
            s.risk_rejected,
            s.orders_submitted,
            s.fills,
            s.total_pnl,
        )

        # Top signal reject reasons
        with self._lock:
            sig_top = dict(self._signal_reject_reasons.most_common(5))
            risk_top = dict(self._risk_reject_reasons.most_common(5))
        if sig_top:
            _log.info("FUNNEL signal_reject_top=%s", sig_top)
        if risk_top:
            _log.info("FUNNEL risk_reject_top=%s", risk_top)

    def log_regime_heatmap(self) -> None:
        """Log regime × time-of-day heatmap of throughput."""
        with self._lock:
            if not self._regime_hour_intents:
                return
            lines = []
            for (regime, hour), count in sorted(self._regime_hour_intents.items()):
                approved = self._regime_hour_approvals.get((regime, hour), 0)
                lines.append(f"  {regime or 'UNK'}@{hour:02d}h intents={count} approved={approved}")
        if lines:
            _log.info("FUNNEL regime_heatmap:\n%s", "\n".join(lines))

    # ── Rolling funnel history for monitor arm ──────────────────

    def record_cycle_snapshot(self) -> None:
        """Take a snapshot of current counters and append to rolling history.

        Call once per monitor heartbeat cycle (every ~10 s).
        """
        s = self.snapshot()
        with self._lock:
            self._cycle_history.append(s)

    def get_funnel_table(self, n: int = 6) -> List[_FunnelSnapshot]:
        """Return the last *n* cycle-level funnel snapshots."""
        with self._lock:
            return list(self._cycle_history)[-n:]

    def get_summary(self) -> dict:
        """Return dict summary for dashboard JSON."""
        s = self.snapshot()
        with self._lock:
            return {
                "funnel": {
                    "candidates_seen": s.candidates_seen,
                    "intents_emitted": s.intents_emitted,
                    "risk_approved": s.risk_approved,
                    "risk_rejected": s.risk_rejected,
                    "orders_submitted": s.orders_submitted,
                    "fills": s.fills,
                    "total_pnl": s.total_pnl,
                },
                "signal_reject_top": dict(self._signal_reject_reasons.most_common(10)),
                "risk_reject_top": dict(self._risk_reject_reasons.most_common(10)),
            }


# Module-level singleton
throughput = ThroughputDashboard()
