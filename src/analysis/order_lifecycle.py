"""Order lifecycle event model for U.T.S.

Observe-only — does not alter strategy logic, filters, or thresholds.
Provides a structured event log with per-symbol state machine validation
and dedup control.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional, Set


# ── Canonical lifecycle events ────────────────────────────────────

class OrderEvent(str, Enum):
    SIGNAL_SCORE             = "SIGNAL_SCORE"
    TRADE_INTENT_CREATED     = "TRADE_INTENT_CREATED"
    RISK_APPROVED            = "RISK_APPROVED"
    RISK_REJECTED            = "RISK_REJECTED"
    ORDER_PLACED             = "ORDER_PLACED"
    ORDER_WORKING            = "ORDER_WORKING"
    ORDER_QUEUED_NEXT_SESSION = "ORDER_QUEUED_NEXT_SESSION"
    ORDER_FILLED             = "ORDER_FILLED"
    ORDER_PARTIALLY_FILLED   = "ORDER_PARTIALLY_FILLED"
    ORDER_CANCELLED          = "ORDER_CANCELLED"
    BRACKET_DEGRADED         = "BRACKET_DEGRADED"
    POSITION_OPEN            = "POSITION_OPEN"
    TRAIL_ACTIVATED          = "TRAIL_ACTIVATED"
    POSITION_CLOSED          = "POSITION_CLOSED"
    SYSTEM_ERROR             = "SYSTEM_ERROR"


# ── State machine transitions ─────────────────────────────────────
# key = current state (None = no prior event for this symbol)
# value = set of valid next events

VALID_TRANSITIONS: Dict[Optional[OrderEvent], Set[OrderEvent]] = {
    None: {
        OrderEvent.SIGNAL_SCORE,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.SIGNAL_SCORE: {
        OrderEvent.TRADE_INTENT_CREATED,
        OrderEvent.SIGNAL_SCORE,           # re-scored in later loop
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.TRADE_INTENT_CREATED: {
        OrderEvent.RISK_APPROVED,
        OrderEvent.RISK_REJECTED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.RISK_APPROVED: {
        OrderEvent.ORDER_PLACED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.RISK_REJECTED: {
        OrderEvent.SIGNAL_SCORE,           # may re-appear next loop
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.ORDER_PLACED: {
        OrderEvent.ORDER_WORKING,
        OrderEvent.ORDER_QUEUED_NEXT_SESSION,
        OrderEvent.ORDER_FILLED,
        OrderEvent.ORDER_CANCELLED,
        OrderEvent.BRACKET_DEGRADED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.BRACKET_DEGRADED: {
        OrderEvent.ORDER_WORKING,
        OrderEvent.ORDER_QUEUED_NEXT_SESSION,
        OrderEvent.ORDER_FILLED,
        OrderEvent.ORDER_CANCELLED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.ORDER_QUEUED_NEXT_SESSION: {
        OrderEvent.ORDER_WORKING,
        OrderEvent.ORDER_FILLED,
        OrderEvent.ORDER_CANCELLED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.ORDER_WORKING: {
        OrderEvent.ORDER_FILLED,
        OrderEvent.ORDER_PARTIALLY_FILLED,
        OrderEvent.ORDER_CANCELLED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.ORDER_PARTIALLY_FILLED: {
        OrderEvent.ORDER_FILLED,
        OrderEvent.ORDER_PARTIALLY_FILLED,  # additional partial fills
        OrderEvent.ORDER_CANCELLED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.ORDER_FILLED: {
        OrderEvent.POSITION_OPEN,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.ORDER_CANCELLED: {
        OrderEvent.SIGNAL_SCORE,           # symbol may re-enter pipeline
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.POSITION_OPEN: {
        OrderEvent.TRAIL_ACTIVATED,
        OrderEvent.POSITION_CLOSED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.TRAIL_ACTIVATED: {
        OrderEvent.POSITION_CLOSED,
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.POSITION_CLOSED: {
        OrderEvent.SIGNAL_SCORE,           # symbol may re-enter pipeline
        OrderEvent.SYSTEM_ERROR,
    },
    OrderEvent.SYSTEM_ERROR: {
        OrderEvent.SIGNAL_SCORE,           # recovery
        OrderEvent.SYSTEM_ERROR,
    },
}


# ── Structured event record ──────────────────────────────────────

@dataclass
class LifecycleEvent:
    """Single lifecycle event record with all applicable fields."""
    timestamp: str
    session_id: str
    event: str
    symbol: str
    order_id: Optional[int] = None
    parent_id: Optional[int] = None
    stop_id: Optional[int] = None
    trail_id: Optional[int] = None
    qty: Optional[int] = None
    entry_price: Optional[float] = None
    stop_price: Optional[float] = None
    trail_amount: Optional[float] = None
    status: Optional[str] = None
    message: Optional[str] = None
    unified_score: Optional[float] = None
    catalyst_score: Optional[float] = None
    quant_score: Optional[float] = None
    gate: Optional[str] = None
    risk_pct: Optional[float] = None
    pnl: Optional[float] = None
    extra: Optional[Dict[str, Any]] = None

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v is not None}


_EVENT_FIELDS = set(LifecycleEvent.__dataclass_fields__.keys()) - {
    "timestamp", "session_id", "event", "symbol", "extra",
}


# ── Lifecycle logger with state machine ───────────────────────────

class LifecycleLogger:
    """Per-session order lifecycle event logger.

    Observe-only — does not modify strategy, filters, or thresholds.
    """

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._events: List[LifecycleEvent] = []
        self._state: Dict[str, OrderEvent] = {}           # symbol → current state
        self._placed_orders: Dict[int, str] = {}           # parent_id → symbol
        self._order_ib_status: Dict[int, str] = {}         # parent_id → last IB status

    # ── Public API ────────────────────────────────────────────

    @property
    def events(self) -> List[LifecycleEvent]:
        return list(self._events)

    def current_state(self, symbol: str) -> Optional[OrderEvent]:
        return self._state.get(symbol)

    def emit(self, event: OrderEvent, symbol: str, **payload) -> LifecycleEvent:
        """Record and console-log a lifecycle event.

        Validates the state transition (advisory — warns but never blocks).
        Suppresses duplicate ORDER_WORKING for the same symbol.
        """
        current = self._state.get(symbol)

        # Dedup: suppress repeated ORDER_WORKING for same symbol
        if event == OrderEvent.ORDER_WORKING and current == OrderEvent.ORDER_WORKING:
            return self._events[-1]

        # Advisory transition check
        allowed = VALID_TRANSITIONS.get(current, set())
        if event not in allowed and event != OrderEvent.SYSTEM_ERROR:
            old_msg = payload.get("message") or ""
            payload["message"] = f"[WARN: {current}→{event}] {old_msg}".strip()

        ts = datetime.now(timezone.utc).isoformat()

        evt_kwargs = {k: v for k, v in payload.items() if k in _EVENT_FIELDS}
        extra_kv = {k: v for k, v in payload.items() if k not in _EVENT_FIELDS}

        evt = LifecycleEvent(
            timestamp=ts,
            session_id=self.session_id,
            event=event.value,
            symbol=symbol,
            **evt_kwargs,
        )
        if extra_kv:
            evt.extra = extra_kv

        self._events.append(evt)
        self._state[symbol] = event

        # Console output — compact one-liner
        detail = " ".join(f"{k}={v}" for k, v in payload.items() if v is not None)
        print(f"[LC:{event.value}] {ts[11:19]} {symbol} {detail}")

        return evt

    # ── Order status tracking ─────────────────────────────────

    def register_order(self, symbol: str, parent_id: int):
        """Track a placed order for IB status polling."""
        self._placed_orders[parent_id] = symbol
        self._order_ib_status[parent_id] = "NEW"

    def poll_order_status(self, open_trades) -> None:
        """Detect IB status transitions for tracked orders.

        Call once per loop iteration; emits ORDER_WORKING,
        ORDER_CANCELLED, ORDER_PARTIALLY_FILLED as needed.
        """
        for trade in open_trades:
            oid = trade.order.orderId
            if oid not in self._placed_orders:
                continue
            sym = self._placed_orders[oid]
            new_st = trade.orderStatus.status
            old_st = self._order_ib_status.get(oid, "NEW")
            if new_st == old_st:
                continue
            self._order_ib_status[oid] = new_st

            if new_st in ("PreSubmitted", "Submitted"):
                if self._state.get(sym) != OrderEvent.ORDER_WORKING:
                    self.emit(OrderEvent.ORDER_WORKING, sym,
                              order_id=oid, status=new_st)
            elif new_st in ("Cancelled", "Inactive"):
                self.emit(OrderEvent.ORDER_CANCELLED, sym,
                          order_id=oid, message=f"IB status={new_st}")

            # Partial fill detection
            filled_qty = getattr(trade.orderStatus, "filled", 0)
            total_qty = getattr(trade.order, "totalQuantity", 0)
            if 0 < filled_qty < total_qty:
                if self._state.get(sym) != OrderEvent.ORDER_PARTIALLY_FILLED:
                    self.emit(OrderEvent.ORDER_PARTIALLY_FILLED, sym,
                              order_id=oid, qty=int(filled_qty),
                              message=f"partial {filled_qty}/{total_qty}")

    # ── Output ────────────────────────────────────────────────

    def events_for_symbol(self, symbol: str) -> List[LifecycleEvent]:
        return [e for e in self._events if e.symbol == symbol]

    def summary(self) -> Dict[str, Any]:
        by_event: Dict[str, int] = {}
        for e in self._events:
            by_event[e.event] = by_event.get(e.event, 0) + 1
        return {
            "session_id": self.session_id,
            "total_events": len(self._events),
            "event_counts": by_event,
            "final_states": {sym: st.value for sym, st in self._state.items()},
            "events": [e.to_dict() for e in self._events],
        }

    def write_json(self, directory: str) -> Optional[str]:
        d = Path(directory)
        d.mkdir(parents=True, exist_ok=True)
        payload = self.summary()
        p = d / f"order_lifecycle_{self.session_id}.json"
        canonical = d / "order_lifecycle_latest.json"
        for fpath in (p, canonical):
            with open(fpath, "w") as f:
                json.dump(payload, f, indent=2, default=str)
        return str(p)

    def print_summary(self):
        s = self.summary()
        print("\n" + "=" * 64)
        print("  ORDER LIFECYCLE SUMMARY")
        print("=" * 64)
        print(f"  session_id    : {self.session_id}")
        print(f"  total_events  : {s['total_events']}")
        print("-" * 64)
        print("  EVENT COUNTS")
        for evt, cnt in sorted(s["event_counts"].items()):
            print(f"    {evt:<30s} : {cnt}")
        print("-" * 64)
        print("  FINAL STATES")
        for sym, state in sorted(s["final_states"].items()):
            print(f"    {sym:<10s} -> {state}")
        print("=" * 64)
