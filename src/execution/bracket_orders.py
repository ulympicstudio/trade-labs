import time
from dataclasses import dataclass
from typing import Optional, Tuple

from ib_insync import IB, Stock, LimitOrder, Order


@dataclass
class BracketParams:
    symbol: str
    qty: int
    entry_limit: float
    take_profit: float
    trail_amount: float
    tif: str = "DAY"


@dataclass
class BracketResult:
    ok: bool
    message: str
    parent_id: Optional[int] = None
    tp_id: Optional[int] = None
    trail_id: Optional[int] = None


def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def place_limit_tp_trail_bracket(
    ib: IB,
    p: BracketParams,
    oca_group: Optional[str] = None
) -> BracketResult:
    """
    3-layer bracket:
      - Parent: BUY LMT (transmit=False)
      - Child A: SELL LMT take-profit (transmit=False)
      - Child B: SELL TRAIL (transmit=True)
    OCO behavior: TP and TRAIL share same OCA group.
    """
    try:
        c = _contract(p.symbol)
        ib.qualifyContracts(c)

        if oca_group is None:
            oca_group = f"OCA_{p.symbol}_{int(time.time())}"

        # Parent (entry)
        parent = LimitOrder("BUY", p.qty, round(p.entry_limit, 2))
        parent.tif = p.tif
        parent.transmit = False

        trade_parent = ib.placeOrder(c, parent)
        ib.sleep(0.2)
        parent_id = parent.orderId

        # Child A: Take profit limit
        tp = LimitOrder("SELL", p.qty, round(p.take_profit, 2))
        tp.parentId = parent_id
        tp.tif = p.tif
        tp.transmit = False
        tp.ocaGroup = oca_group
        tp.ocaType = 1  # CANCEL_WITH_BLOCK

        trade_tp = ib.placeOrder(c, tp)
        ib.sleep(0.2)
        tp_id = tp.orderId

        # Child B: Trailing stop
        # IB trailing stop uses orderType='TRAIL' with auxPrice as trail amount (in dollars).
        trail = Order()
        trail.action = "SELL"
        trail.totalQuantity = p.qty
        trail.orderType = "TRAIL"
        trail.auxPrice = float(round(p.trail_amount, 4))  # trailing amount in $
        trail.parentId = parent_id
        trail.tif = p.tif
        trail.ocaGroup = oca_group
        trail.ocaType = 1
        trail.transmit = True  # final child transmits whole bracket

        trade_trail = ib.placeOrder(c, trail)
        ib.sleep(0.2)
        trail_id = trail.orderId

        return BracketResult(
            ok=True,
            message="Bracket submitted to IB (paper).",
            parent_id=parent_id,
            tp_id=tp_id,
            trail_id=trail_id
        )
    except Exception as e:
        return BracketResult(ok=False, message=f"Bracket failed: {e}")
