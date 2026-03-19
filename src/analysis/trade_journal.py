"""Automatic paper trade journal for U.T.S.

Observe-only — does not alter execution logic, filters, or thresholds.
Records every attempted or completed trade in a structured journal for
later review, analytics, and model training.
"""

from __future__ import annotations

import csv
import json
import os
from dataclasses import dataclass, field, fields, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional


# ── Column order for CSV output ───────────────────────────────────

CSV_COLUMNS = [
    "trade_key",
    "session_id",
    "status",
    "timestamp_signal",
    "timestamp_intent",
    "timestamp_order_submitted",
    "timestamp_fill",
    "timestamp_exit",
    "symbol",
    "side",
    "qty",
    "entry_limit",
    "entry_fill",
    "stop_price",
    "trail_amount",
    "trail_activation_price",
    "exit_price",
    "exit_reason",
    "hold_seconds",
    "realized_pnl",
    "unrealized_pnl_at_close",
    "unified_score",
    "catalyst_score",
    "quant_score",
    "vol_accel",
    "atr_pct",
    "rs_30m_delta",
    "momentum_30m",
    "momentum_60m",
    "adv20_dollars",
    "regime",
    "gate_type",
    "bracket_degraded",
    "queued_next_session",
    "parent_id",
    "stop_id",
    "trail_id",
]


@dataclass
class TradeRecord:
    """Single journal row — mutable across lifecycle stages."""
    trade_key: str = ""
    session_id: str = ""
    status: str = ""                        # submitted_unfilled | open | closed
    timestamp_signal: str = ""
    timestamp_intent: str = ""
    timestamp_order_submitted: str = ""
    timestamp_fill: str = ""
    timestamp_exit: str = ""
    symbol: str = ""
    side: str = "BUY"
    qty: int = 0
    entry_limit: float = 0.0
    entry_fill: float = 0.0
    stop_price: float = 0.0
    trail_amount: float = 0.0
    trail_activation_price: float = 0.0
    exit_price: float = 0.0
    exit_reason: str = ""
    hold_seconds: float = 0.0
    realized_pnl: float = 0.0
    unrealized_pnl_at_close: float = 0.0
    unified_score: float = 0.0
    catalyst_score: float = 0.0
    quant_score: float = 0.0
    vol_accel: float = 0.0
    atr_pct: float = 0.0
    rs_30m_delta: float = 0.0
    momentum_30m: float = 0.0
    momentum_60m: float = 0.0
    adv20_dollars: float = 0.0
    regime: str = ""
    gate_type: str = ""
    bracket_degraded: bool = False
    queued_next_session: bool = False
    parent_id: Optional[int] = None
    stop_id: Optional[int] = None
    trail_id: Optional[int] = None

    def to_csv_row(self) -> Dict[str, Any]:
        d = asdict(self)
        # Booleans → lowercase strings for CSV clarity
        d["bracket_degraded"] = str(d["bracket_degraded"]).lower()
        d["queued_next_session"] = str(d["queued_next_session"]).lower()
        # None → empty string for clean CSV
        return {k: ("" if v is None else v) for k, v in d.items()}

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v or v == 0 or v is False}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _trade_key(session_id: str, symbol: str) -> str:
    """Stable key: one record per symbol per session."""
    return f"{session_id}:{symbol}"


# ── Journal manager ───────────────────────────────────────────────

class TradeJournal:
    """Append-safe trade journal that tracks records across lifecycle stages.

    Observe-only — does not modify execution logic.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._records: Dict[str, TradeRecord] = {}   # trade_key → record

    # ── Core CRUD ─────────────────────────────────────────────

    def _get_or_create(self, symbol: str) -> TradeRecord:
        key = _trade_key(self.session_id, symbol)
        if key not in self._records:
            self._records[key] = TradeRecord(
                trade_key=key,
                session_id=self.session_id,
                symbol=symbol,
                status="pending",
            )
        return self._records[key]

    def create_trade_record(
        self,
        symbol: str,
        *,
        unified_score: float = 0.0,
        catalyst_score: float = 0.0,
        quant_score: float = 0.0,
        gate_type: str = "",
        vol_accel: float = 0.0,
        atr_pct: float = 0.0,
        rs_30m_delta: float = 0.0,
        momentum_30m: float = 0.0,
        adv20_dollars: float = 0.0,
        regime: str = "",
    ) -> TradeRecord:
        """Create (or update) the record at SIGNAL stage."""
        rec = self._get_or_create(symbol)
        rec.timestamp_signal = _now_iso()
        rec.unified_score = unified_score
        rec.catalyst_score = catalyst_score
        rec.quant_score = quant_score
        rec.gate_type = gate_type
        rec.vol_accel = vol_accel
        rec.atr_pct = atr_pct
        rec.rs_30m_delta = rs_30m_delta
        rec.momentum_30m = momentum_30m
        rec.adv20_dollars = adv20_dollars
        rec.regime = regime
        return rec

    def update_trade_record(self, symbol: str, **kwargs) -> TradeRecord:
        """Update arbitrary fields on an existing record."""
        rec = self._get_or_create(symbol)
        for k, v in kwargs.items():
            if hasattr(rec, k):
                setattr(rec, k, v)
        return rec

    def finalize_trade_record(self, symbol: str, **kwargs) -> TradeRecord:
        """Finalize a record at session end (set status, compute hold_seconds)."""
        rec = self._get_or_create(symbol)
        for k, v in kwargs.items():
            if hasattr(rec, k):
                setattr(rec, k, v)

        # Compute hold_seconds from fill → exit if both present
        if rec.timestamp_fill and rec.timestamp_exit:
            try:
                t_fill = datetime.fromisoformat(rec.timestamp_fill)
                t_exit = datetime.fromisoformat(rec.timestamp_exit)
                rec.hold_seconds = round((t_exit - t_fill).total_seconds(), 1)
            except (ValueError, TypeError):
                pass
        return rec

    # ── Convenience: stage-specific updates ───────────────────

    def record_intent(self, symbol: str, *, qty: int, entry_limit: float,
                      stop_price: float, trail_activation_price: float,
                      risk_pct: float) -> TradeRecord:
        rec = self._get_or_create(symbol)
        rec.timestamp_intent = _now_iso()
        rec.qty = qty
        rec.entry_limit = entry_limit
        rec.stop_price = stop_price
        rec.trail_activation_price = trail_activation_price
        return rec

    def record_order_submitted(self, symbol: str, *, parent_id=None,
                               stop_id=None, trail_id=None,
                               degraded: bool = False,
                               queued: bool = False,
                               status: str = "submitted_unfilled") -> TradeRecord:
        rec = self._get_or_create(symbol)
        rec.timestamp_order_submitted = _now_iso()
        rec.parent_id = parent_id
        rec.stop_id = stop_id
        rec.trail_id = trail_id
        rec.bracket_degraded = degraded
        rec.queued_next_session = queued
        rec.status = status
        return rec

    def record_fill(self, symbol: str, *, entry_fill: float,
                    qty: int) -> TradeRecord:
        rec = self._get_or_create(symbol)
        rec.timestamp_fill = _now_iso()
        rec.entry_fill = entry_fill
        if qty:
            rec.qty = qty
        rec.status = "open"
        return rec

    def record_trail_activated(self, symbol: str, *,
                               trail_amount: float,
                               trail_id=None) -> TradeRecord:
        rec = self._get_or_create(symbol)
        rec.trail_amount = trail_amount
        if trail_id is not None:
            rec.trail_id = trail_id
        return rec

    def record_exit(self, symbol: str, *, exit_price: float = 0.0,
                    exit_reason: str = "", realized_pnl: float = 0.0) -> TradeRecord:
        rec = self._get_or_create(symbol)
        rec.timestamp_exit = _now_iso()
        rec.exit_price = exit_price
        rec.exit_reason = exit_reason
        rec.realized_pnl = realized_pnl
        rec.status = "closed"
        # Compute hold_seconds
        if rec.timestamp_fill:
            try:
                t_fill = datetime.fromisoformat(rec.timestamp_fill)
                t_exit = datetime.fromisoformat(rec.timestamp_exit)
                rec.hold_seconds = round((t_exit - t_fill).total_seconds(), 1)
            except (ValueError, TypeError):
                pass
        return rec

    # ── Batch finalize at shutdown ────────────────────────────

    def finalize_session(self, closed_trades: List[dict],
                         open_symbols: set) -> None:
        """Finalize all records at session end.

        - Match closed_trades from trade history to update status/pnl.
        - Mark still-open positions as status=open.
        - Leave unfilled orders as status=submitted_unfilled.
        """
        if not closed_trades:
            closed_trades = []
        if not open_symbols:
            open_symbols = set()
        # Update from closed trades history
        for ct in closed_trades:
            sym = ct.get("symbol", "")
            if not sym:
                continue
            rec = self._get_or_create(sym)
            if rec.status == "closed":
                continue  # already finalized
            rec.status = "closed"
            rec.exit_reason = ct.get("close_reason", "unknown")
            rec.realized_pnl = ct.get("pnl", 0.0)
            rec.timestamp_exit = ct.get("close_time") or ct.get("ts") or _now_iso()
            rec.exit_price = ct.get("exit_price", 0.0)
            # Compute hold
            if rec.timestamp_fill and rec.timestamp_exit:
                try:
                    t_fill = datetime.fromisoformat(rec.timestamp_fill)
                    t_exit = datetime.fromisoformat(rec.timestamp_exit)
                    rec.hold_seconds = round((t_exit - t_fill).total_seconds(), 1)
                except (ValueError, TypeError):
                    pass

        # Mark symbols that are still open
        for sym in open_symbols:
            rec = self._get_or_create(sym)
            if rec.status not in ("closed",):
                rec.status = "open"

        # Any remaining unfinalized records are submitted_unfilled
        for rec in self._records.values():
            if rec.status == "pending":
                if rec.timestamp_order_submitted:
                    rec.status = "submitted_unfilled"
                else:
                    rec.status = "signal_only"

    # ── Output ────────────────────────────────────────────────

    @property
    def records(self) -> List[TradeRecord]:
        return list(self._records.values())

    def write_csv(self, directory: str = "logs") -> Optional[str]:
        """Append records to logs/trade_journal.csv (creates header if new).

        Returns path to the file.
        """
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        p = d / "trade_journal.csv"

        file_exists = p.exists() and p.stat().st_size > 0
        with open(p, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS,
                                    extrasaction="ignore")
            if not file_exists:
                writer.writeheader()
            for rec in self._records.values():
                writer.writerow(rec.to_csv_row())
        return str(p)

    def write_json(self, directory: str = "logs") -> Optional[str]:
        """Write session-specific JSON journal."""
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        p = d / f"trade_journal_{self.session_id}.json"
        payload = {
            "session_id": self.session_id,
            "record_count": len(self._records),
            "records": [rec.to_dict() for rec in self._records.values()],
        }
        with open(p, "w") as f:
            json.dump(payload, f, indent=2, default=str)
        return str(p)

    def print_summary(self):
        recs = list(self._records.values())
        if not recs:
            return
        by_status: Dict[str, int] = {}
        for r in recs:
            by_status[r.status] = by_status.get(r.status, 0) + 1

        print("\n" + "=" * 64)
        print("  TRADE JOURNAL SUMMARY")
        print("=" * 64)
        print(f"  session_id    : {self.session_id}")
        print(f"  total_records : {len(recs)}")
        print("-" * 64)
        for status, count in sorted(by_status.items()):
            print(f"    {status:<25s} : {count}")
        print("-" * 64)
        for r in recs:
            pnl_str = f"pnl=${r.realized_pnl:+.2f}" if r.status == "closed" else ""
            fill_str = f"fill=${r.entry_fill:.2f}" if r.entry_fill else f"lmt=${r.entry_limit:.2f}"
            print(f"    {r.symbol:<8s} {r.status:<22s} {fill_str} {pnl_str}")
        print("=" * 64)
