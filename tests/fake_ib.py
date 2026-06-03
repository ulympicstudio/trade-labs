"""Mocked IBKR harness for CI-friendly execution tests.

No network, no broker, no ib_insync IB() connection. ``FakeIB`` records every
``placeOrder`` call and exposes programmable ``orderStatus`` / ``positions()``
/ ``openTrades()`` / ``trades()`` / ``reqHistoricalData`` / ``reqContractDetails``
so tests can assert leg wiring, transmit chaining, fill accounting, and
degraded-bracket cleanup.

The fake mirrors only the surface area the production code touches.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class FakeContractDetails:
    minTick: float = 0.01


@dataclass
class FakeContract:
    symbol: str
    secType: str = "STK"
    exchange: str = "SMART"
    currency: str = "USD"
    conId: int = 0


@dataclass
class FakeLogEntry:
    status: str


@dataclass
class FakeOrderStatus:
    status: str = "PreSubmitted"
    filled: float = 0.0
    avgFillPrice: float = 0.0


@dataclass
class FakeTrade:
    contract: FakeContract
    order: object
    orderStatus: FakeOrderStatus = field(default_factory=FakeOrderStatus)
    log: List[FakeLogEntry] = field(default_factory=list)
    cancelled: bool = False


@dataclass
class FakePosition:
    contract: FakeContract
    position: float
    avgCost: float = 0.0


class FakeIB:
    """A record-and-replay stand-in for ``ib_insync.IB``."""

    def __init__(
        self,
        min_tick: float = 0.01,
        default_status: str = "PreSubmitted",
        reject_order_ids: Optional[set] = None,
        reject_predicate=None,
    ):
        self._id_counter = itertools.count(1)
        self.min_tick = min_tick
        self.default_status = default_status
        # If a placed order matches, it gets an Inactive/rejected status.
        self.reject_order_ids = reject_order_ids or set()
        self.reject_predicate = reject_predicate

        self.placed_orders: List[object] = []        # in call order
        self.placed_trades: List[FakeTrade] = []      # parallel to placed_orders
        self.cancelled_orders: List[object] = []
        self._open_trades: List[FakeTrade] = []
        self._positions: List[FakePosition] = []
        self._connected = True
        self.reqContractDetails_calls = 0
        self.historical_calls = 0

    # ── connection ──
    def isConnected(self) -> bool:
        return self._connected

    def disconnect(self) -> None:
        self._connected = False

    def sleep(self, _secs: float = 0.0) -> None:
        pass

    # ── contracts ──
    def qualifyContracts(self, *contracts):
        for i, c in enumerate(contracts):
            if getattr(c, "conId", 0) in (0, None):
                c.conId = 1000 + i
        return list(contracts)

    def reqContractDetails(self, _contract):
        self.reqContractDetails_calls += 1
        return [FakeContractDetails(minTick=self.min_tick)]

    def reqHistoricalData(self, *_args, **_kwargs):
        self.historical_calls += 1
        return []

    # ── orders ──
    def placeOrder(self, contract, order) -> FakeTrade:
        order.orderId = next(self._id_counter)
        rejected = (
            order.orderId in self.reject_order_ids
            or (self.reject_predicate is not None and self.reject_predicate(order))
        )
        status = "Inactive" if rejected else self.default_status
        trade = FakeTrade(
            contract=contract,
            order=order,
            orderStatus=FakeOrderStatus(status=status),
            log=[FakeLogEntry(status=status)],
        )
        self.placed_orders.append(order)
        self.placed_trades.append(trade)
        self._open_trades.append(trade)
        return trade

    def cancelOrder(self, order) -> None:
        self.cancelled_orders.append(order)
        for tr in self._open_trades:
            if tr.order is order:
                tr.cancelled = True
                tr.orderStatus.status = "Cancelled"

    # ── state queries ──
    def openTrades(self) -> List[FakeTrade]:
        return [t for t in self._open_trades if not t.cancelled]

    def trades(self) -> List[FakeTrade]:
        return list(self.placed_trades)

    def positions(self) -> List[FakePosition]:
        return list(self._positions)

    # ── test helpers ──
    def set_position(self, symbol: str, qty: float, avg_cost: float = 0.0) -> None:
        self._positions = [p for p in self._positions if p.contract.symbol != symbol]
        if qty != 0:
            self._positions.append(
                FakePosition(FakeContract(symbol=symbol), qty, avg_cost)
            )

    def fill_order(self, order_id: int, filled: float, avg_price: float) -> None:
        for tr in self.placed_trades:
            if getattr(tr.order, "orderId", None) == order_id:
                tr.orderStatus.status = "Filled"
                tr.orderStatus.filled = filled
                tr.orderStatus.avgFillPrice = avg_price
                tr.log.append(FakeLogEntry(status="Filled"))

    def transmitted_orders(self) -> List[object]:
        return [o for o in self.placed_orders if getattr(o, "transmit", False)]
