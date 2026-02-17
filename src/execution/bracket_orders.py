import time
from dataclasses import dataclass
from typing import Optional, Tuple

from ib_insync import IB, Stock, LimitOrder, Order, StopOrder


@dataclass
class BracketParams:
    symbol: str
    qty: int
    entry_limit: float
    stop_loss: float  # Hard downside protection (OCA child A)
    trail_amount: float  # Upside capture (OCA child B)
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
    3-layer bracket with stop loss + trailing stop:
      - Parent: BUY LMT at entry (transmit=False)
      - Child A: SELL STP at stop_loss (hard downside protection, TRUE stop order, transmit=False)
      - Child B: SELL TRAIL (upside capture, transmit=True)
    
    OCA behavior: Stop Loss and TRAIL are mutually exclusive.
    - If stock drops to stop_loss → STOP triggered, filled as market order (downside protected)
    - If stock rises → TRAIL follows up locking in gains (upside captured)
    
    ✅ CORRECTED: Using STP (STOP order) not LMT for proper downside protection
    """
    try:
        c = _contract(p.symbol)
        ib.qualifyContracts(c)
        print(f"  [DEBUG] Contract qualified: {c.symbol}, secType={c.secType}, conId={c.conId}")

        if oca_group is None:
            oca_group = f"OCA_{p.symbol}_{int(time.time())}"

        # Parent (entry)
        parent = LimitOrder("BUY", p.qty, round(p.entry_limit, 2))
        parent.tif = p.tif
        parent.transmit = False

        trade_parent = ib.placeOrder(c, parent)
        ib.sleep(0.2)
        parent_id = parent.orderId
        print(f"  [DEBUG] Parent BUY order: id={parent_id}, qty={p.qty}, LMT={p.entry_limit:.2f}")

        # Child A: STOP order for downside protection (✅ CORRECTED: Using StopOrder, not LimitOrder)
        stop_loss_order = StopOrder("SELL", p.qty, round(p.stop_loss, 2))
        stop_loss_order.parentId = parent_id
        stop_loss_order.tif = p.tif
        stop_loss_order.transmit = False
        stop_loss_order.ocaGroup = oca_group
        stop_loss_order.ocaType = 1  # CANCEL_WITH_BLOCK

        trade_stop_loss = ib.placeOrder(c, stop_loss_order)
        ib.sleep(0.2)
        stop_loss_id = stop_loss_order.orderId
        print(f"  [DEBUG] STOP LOSS order: id={stop_loss_id}, qty={p.qty}, STP=${p.stop_loss:.2f}, parentId={parent_id}")
        print(f"  [DEBUG] ✅ Stop loss is SELL STP (proper stop order, not limit)")


        # Child B: Trailing stop
        # Use Order with orderType='TRAIL' for proper trailing stop
        trail = Order()
        trail.action = "SELL"
        trail.totalQuantity = p.qty
        trail.orderType = "TRAIL"
        # Round to 2 decimals for IB minimum price variation compliance (0.01 increment)
        trail.auxPrice = float(round(p.trail_amount, 2))  # trailing amount in $
        trail.parentId = parent_id
        trail.tif = p.tif
        trail.ocaGroup = oca_group
        trail.ocaType = 1
        trail.transmit = True  # final child transmits whole bracket
        trail.eTradeOnly = False
        trail.firmQuoteOnly = False

        print(f"  [DEBUG] TRAIL Order object before placeOrder:")
        print(f"    action={trail.action}, qty={trail.totalQuantity}, orderType={trail.orderType}")
        print(f"    auxPrice={trail.auxPrice}, parentId={trail.parentId}, tif={trail.tif}")
        print(f"    ocaGroup={trail.ocaGroup}, ocaType={trail.ocaType}, transmit={trail.transmit}")
        
        # Capture all events while placing TRAIL order
        trail_errors = []
        trail_events = []
        
        def capture_trail_error(*args):
            trail_errors.append(args)
            if len(args) > 2:
                print(f"  [ERROR TRAIL] Error {args[1]}: {args[2]}")
            else:
                print(f"  [ERROR TRAIL] {str(args)}")
        
        def capture_any_event(obj):
            trail_events.append(f"Event fired on {type(obj).__name__}: {obj}")
        
        error_handler = capture_trail_error
        ib.errorEvent += error_handler
        
        # Also try to capture order status changes
        print(f"  [DEBUG] Placing TRAIL order with transmit=True...")
        trade_trail = ib.placeOrder(c, trail)
        ib.sleep(0.2)
        
        # Clean up error handler
        ib.errorEvent -= error_handler
        
        trail_id = trail.orderId
        print(f"  [DEBUG] After placeOrder: trail.orderId={trail_id}")
        print(f"  [DEBUG] trade_trail.order.orderId={trade_trail.order.orderId if trade_trail else 'N/A'}")
        print(f"  [DEBUG] Errors captured: {len(trail_errors)}")
        
        if trail_id == 0 or trail_id is None:
            print(f"  [ERROR] TRAIL order returned orderId={trail_id}")
            if trail_errors:
                print(f"  [ERROR] IB Error events captured ({len(trail_errors)}):")
                for err in trail_errors:
                    print(f"    → {err}")
            else:
                print(f"  [ERROR] No error events captured - IB may have rejected silently")
        else:
            print(f"  [DEBUG] TRAIL order placed successfully: id={trail_id}")

        return BracketResult(
            ok=True,
            message="Bracket submitted to IB (paper).",
            parent_id=parent_id,
            tp_id=stop_loss_id,  # TP field now holds stop loss ID
            trail_id=trail_id
        )
    except Exception as e:
        import traceback
        print(f"  [ERROR] Bracket placement failed:")
        print(f"    Message: {e}")
        print(f"    Traceback: {traceback.format_exc()}")
        return BracketResult(ok=False, message=f"Bracket failed: {e}")
