"""Order execution helpers (simple paper/live stub)."""
from dataclasses import dataclass
from typing import Optional


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
    symbol: str
    side: str
    quantity: int
    order_type: str
    stop_loss: Optional[float] = None


def place_order(req: OrderRequest, mode: str = "PAPER") -> OrderResult:
    """Place an order (stub).

    - `mode` defaults to 'PAPER'.
    - Returns an `OrderResult` dataclass instance to make repr friendly.
    """
    # Simulate success in PAPER mode and return a structured result.
    return OrderResult(
        ok=True,
        mode=mode,
        symbol=req.symbol,
        side=req.side,
        quantity=req.quantity,
        order_type=req.order_type,
        stop_loss=req.stop_loss,
    )


def send_order(order: dict) -> OrderResult:
    """Compatibility stub for older callers returning OrderResult.

    Accepts a dict and maps fields where possible.
    """
    return OrderResult(
        ok=True,
        mode=order.get("mode", "PAPER"),
        symbol=order.get("symbol", ""),
        side=order.get("side", ""),
        quantity=order.get("quantity", 0),
        order_type=order.get("order_type", ""),
        stop_loss=order.get("stop_loss"),
    )
