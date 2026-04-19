"""
Adapter layer: bus schemas  →  existing execution code.

Translates an :class:`~src.schemas.messages.OrderPlan` (received over
the event bus) into the :class:`~src.execution.orders.OrderRequest`
expected by the current ``place_order()`` function, and converts the
:class:`~src.execution.orders.OrderResult` back into an
:class:`~src.schemas.messages.OrderEvent` for publication.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from src.schemas.messages import OrderEvent, OrderPlan

# Import the existing execution code — *not* re-implementing logic.
from src.execution.orders import OrderRequest as LegacyOrderRequest
from src.execution.orders import OrderResult as LegacyOrderResult


# ── OrderPlan → legacy OrderRequest ──────────────────────────────────

def plan_to_order_request(plan: OrderPlan) -> LegacyOrderRequest:
    """Convert an ``OrderPlan`` from the bus into a legacy ``OrderRequest``.

    Mapping decisions:
    * ``side`` — derived from ``entry_type``; the plan itself doesn't
      carry a side because the upstream ``TradeIntent.direction`` already
      determined it.  For now we default to ``"BUY"``; the risk arm
      should set ``plan.trail_params["side"]`` if it needs ``"SELL"``.
    * ``order_type`` — mapped from ``plan.entry_type``.
    * ``stop_loss`` — passed through from ``plan.stop_price``.
    """
    side = getattr(plan, "direction", None) or plan.trail_params.get("side", "BUY")
    if side not in ("BUY", "SELL"):
        raise ValueError(f"Invalid side '{side}' in OrderPlan for {plan.symbol}")

    # Map entry_type to legacy order_type tokens
    order_type_map = {
        "MKT": "MKT",
        "LMT": "LMT",
        "STP_LMT": "LMT",
    }
    order_type = order_type_map.get(plan.entry_type, "MKT")

    stop_loss: Optional[float] = plan.stop_price if plan.stop_price else None

    return LegacyOrderRequest(
        symbol=plan.symbol,
        side=side,
        quantity=plan.qty,
        order_type=order_type,
        stop_loss=stop_loss,
    )


# ── legacy OrderResult → OrderEvent ─────────────────────────────────

def result_to_order_event(
    result: LegacyOrderResult,
    plan: OrderPlan,
) -> OrderEvent:
    """Convert the response from ``place_order()`` to a bus ``OrderEvent``.

    The event bus needs a simple status update that other arms can
    consume.  We derive ``event_type`` from ``result.ok``.
    """
    if result.ok:
        event_type = "SUBMITTED"
        status = "submitted"
    else:
        # The legacy code returns ok=False for blocks/rejections.
        event_type = "REJECTED"
        status = "rejected"

    order_id = ""
    if result.parent_order_id is not None:
        order_id = str(result.parent_order_id)

    return OrderEvent(
        symbol=result.symbol,
        ts=datetime.now(timezone.utc),
        event_type=event_type,
        order_id=order_id,
        status=status,
        filled_qty=0,             # not yet filled at submission time
        avg_fill_price=0.0,
    )
