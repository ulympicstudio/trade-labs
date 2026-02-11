"""Order execution helpers (simple paper/live stub)."""
from dataclasses import dataclass, asdict
from typing import Optional


@dataclass
class OrderRequest:
    symbol: str
    side: str
    quantity: int
    order_type: str
    stop_loss: Optional[float] = None


def place_order(req: OrderRequest, mode: str = "PAPER") -> dict:
    """Place an order (stub).

    - `mode` defaults to 'PAPER'.
    - Returns a simple confirmation dict with `ok` flag.
    """
    # In a real implementation this would translate `req` into
    # venue-specific API calls. Here we simulate success in PAPER mode.
    confirmation = {
        "ok": True,
        "mode": mode,
        "order": asdict(req),
    }
    return confirmation


def send_order(order: dict) -> dict:
    """Compatibility stub for older callers.

    Delegates to `place_order` semantics by returning a confirmation-like dict.
    """
    return {"ok": True, "mode": "PAPER", "order": order}
