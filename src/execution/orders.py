from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ib_insync import IB, Stock, MarketOrder, StopOrder

from config.runtime import is_paper, execution_backend, is_armed

FINGERPRINT = "ORDERS_PY_ARM_GATE_V1"


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    order_type: str
    stop_loss: Optional[float] = None


@dataclass
class OrderResult:
    ok: bool
    mode: str
    backend: str
    armed: bool
    symbol: str
    side: str
    quantity: int
    order_type: str
    stop_loss: Optional[float]
    timestamp: str
    message: str
    parent_order_id: Optional[int] = None
    stop_order_id: Optional[int] = None
    fingerprint: str = FINGERPRINT


def _make_contract(symbol: str):
    return Stock(symbol, "SMART", "USD")


def place_order(req: OrderRequest, ib: Optional[IB] = None) -> OrderResult:
    ts = datetime.utcnow().isoformat()
    mode = "PAPER" if is_paper() else "LIVE"
    backend = execution_backend()
    armed = is_armed()

    # Never allow LIVE
    if not is_paper():
        return OrderResult(False, mode, backend, armed, req.symbol, req.side, req.quantity,
                          req.order_type, req.stop_loss, ts, "LIVE mode blocked.")

    # SIM is always safe
    if backend == "SIM":
        return OrderResult(True, mode, backend, armed, req.symbol, req.side, req.quantity,
                          req.order_type, req.stop_loss, ts, "SIM order accepted (no broker submission).")

    # IB requires explicit ARM switch
    if backend == "IB" and not armed:
        return OrderResult(False, mode, backend, armed, req.symbol, req.side, req.quantity,
                          req.order_type, req.stop_loss, ts,
                          "BLOCKED: TRADE_LABS_ARMED=0. Set TRADE_LABS_ARMED=1 to allow IB paper orders.")

    if backend == "IB" and ib is None:
        return OrderResult(False, mode, backend, armed, req.symbol, req.side, req.quantity,
                          req.order_type, req.stop_loss, ts,
                          "IB backend requires an active IB connection passed in.")

    # ---- REAL IB PAPER ORDER SUBMISSION ----
    contract = _make_contract(req.symbol)
    ib.qualifyContracts(contract)

    action = req.side.upper()
    qty = int(req.quantity)

    parent = MarketOrder(action, qty)
    ib.placeOrder(contract, parent)
    ib.sleep(1.0)
    parent_id = parent.orderId

    stop_id = None
    if req.stop_loss is not None:
        stop_price = float(req.stop_loss)
        stop_action = "SELL" if action == "BUY" else "BUY"
        stop_order = StopOrder(stop_action, qty, stop_price)
        ib.placeOrder(contract, stop_order)
        ib.sleep(1.0)
        stop_id = stop_order.orderId

    return OrderResult(True, mode, backend, armed, req.symbol, req.side, req.quantity,
                      req.order_type, req.stop_loss, ts,
                      "IB PAPER order submitted (check TWS).",
                      parent_order_id=parent_id, stop_order_id=stop_id)
