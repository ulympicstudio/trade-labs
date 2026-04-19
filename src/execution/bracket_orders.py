import logging
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from ib_insync import IB, Stock, LimitOrder, Order, StopOrder

log = logging.getLogger("bracket_orders")


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
    """Canonical result of a bracket order submission.

    Fields:
        ok       – True if the bracket was accepted by the broker.
        message  – Human-readable status string.
        parent_id – Order ID of the entry (LMT BUY) leg.
        stop_id  – Order ID of the stop-loss (STP SELL) child.
        trail_id – Order ID of the trailing-stop (TRAIL SELL) child.
        degraded – True if one child leg failed but entry was accepted.
    """
    ok: bool
    message: str
    parent_id: Optional[int] = None
    stop_id: Optional[int] = None
    trail_id: Optional[int] = None
    degraded: bool = False

    def __getattr__(self, name: str):
        # Backward-compatible alias: tp_id -> stop_id
        if name == "tp_id":
            return self.stop_id
        raise AttributeError(f"'{type(self).__name__}' has no attribute '{name}'")


def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def place_limit_tp_trail_bracket(
    ib: IB,
    p: BracketParams,
    oca_group: Optional[str] = None
) -> BracketResult:
    """
    Bracket with stop loss, optionally with trailing stop child:
      - Parent: BUY LMT at entry (transmit=False)
      - Child A: SELL STP at stop_loss (hard downside, transmit depends on trail)
      - Child B (optional): SELL TRAIL (upside capture, only if trail_amount > 0)

    When trail_amount <= 0 the bracket is a 2-leg (parent + stop) and the
    trailing stop is expected to be activated later via place_trailing_stop()
    after the entry fills and price appreciates.
    """
    try:
        c = _contract(p.symbol)
        ib.qualifyContracts(c)
        log.debug("Contract qualified: %s, secType=%s, conId=%s", c.symbol, c.secType, c.conId)

        include_trail = p.trail_amount > 0

        if oca_group is None and include_trail:
            oca_group = f"OCA_{p.symbol}_{int(time.time())}"

        # Parent (entry)
        parent = LimitOrder("BUY", p.qty, round(p.entry_limit, 2))
        parent.tif = p.tif
        parent.transmit = False

        trade_parent = ib.placeOrder(c, parent)
        ib.sleep(0.2)
        parent_id = parent.orderId
        log.debug("Parent BUY order: id=%s, qty=%s, LMT=%.2f", parent_id, p.qty, p.entry_limit)

        # Child A: STOP order for downside protection
        stop_loss_order = StopOrder("SELL", p.qty, round(p.stop_loss, 2))
        stop_loss_order.parentId = parent_id
        stop_loss_order.tif = p.tif
        # If no trail child follows, this is the last leg and must transmit.
        stop_loss_order.transmit = not include_trail
        if include_trail:
            stop_loss_order.ocaGroup = oca_group
            stop_loss_order.ocaType = 1  # CANCEL_WITH_BLOCK

        trade_stop_loss = ib.placeOrder(c, stop_loss_order)
        ib.sleep(0.2)
        stop_loss_id = stop_loss_order.orderId
        log.debug("STOP LOSS order: id=%s, qty=%s, STP=$%.2f, parentId=%s", stop_loss_id, p.qty, p.stop_loss, parent_id)

        trail_id = None

        if include_trail:
            # Child B: Trailing stop
            trail = Order()
            trail.action = "SELL"
            trail.totalQuantity = p.qty
            trail.orderType = "TRAIL"
            trail.auxPrice = float(round(p.trail_amount, 2))  # trailing amount in $
            # trailStopPrice: initial stop reference for child orders.
            # IB requires this for TRAIL children attached to an unfilled parent.
            trail.trailStopPrice = float(round(p.entry_limit - p.trail_amount, 2))
            trail.parentId = parent_id
            trail.tif = p.tif
            trail.ocaGroup = oca_group
            trail.ocaType = 1
            trail.transmit = True  # final child transmits whole bracket
            trail.eTradeOnly = False
            trail.firmQuoteOnly = False

            log.debug("TRAIL Order: auxPrice=%s, trailStopPrice=%s, parentId=%s", trail.auxPrice, trail.trailStopPrice, parent_id)

            # Capture IB error events during trail placement
            trail_errors = []
            def capture_trail_error(*args):
                trail_errors.append(args)
                if len(args) > 2:
                    log.warning("TRAIL error %s: %s", args[1], args[2])

            ib.errorEvent += capture_trail_error
            trade_trail = ib.placeOrder(c, trail)
            ib.sleep(0.2)
            ib.errorEvent -= capture_trail_error

            trail_id = trail.orderId
            log.debug("TRAIL order: id=%s, errors=%s", trail_id, len(trail_errors))
        else:
            log.debug("No trail child (trail_amount=%s); 2-leg bracket submitted.", p.trail_amount)

        # Detect degraded bracket (entry ok but a child leg failed)
        _degraded = False
        _parts = []
        if stop_loss_id in (0, None):
            _degraded = True
            _parts.append("stop_loss")
        if include_trail and trail_id in (0, None):
            _degraded = True
            _parts.append("trail")

        _msg = "Bracket submitted to IB (paper)."
        if not include_trail:
            _msg = "2-leg bracket submitted (parent + stop); trail deferred."
        if _degraded:
            _msg += f" DEGRADED: missing child legs {_parts}"
            log.warning(
                "bracket_degraded symbol=%s missing=%s parent_id=%s — cancelling parent",
                p.symbol, _parts, parent_id,
            )
            # Cancel the parent order to prevent an unprotected position
            try:
                ib.cancelOrder(trade_parent.order)
                ib.sleep(0.3)
                log.warning("bracket_parent_cancelled symbol=%s parent_id=%s", p.symbol, parent_id)
            except Exception as cancel_err:
                log.error("bracket_cancel_failed symbol=%s err=%s", p.symbol, cancel_err)
            return BracketResult(
                ok=False,
                message=_msg,
                parent_id=parent_id,
                stop_id=stop_loss_id,
                trail_id=trail_id,
                degraded=True,
            )

        return BracketResult(
            ok=True,
            message=_msg,
            parent_id=parent_id,
            stop_id=stop_loss_id,
            trail_id=trail_id,
            degraded=False,
        )
    except Exception as e:
        log.exception("Bracket placement failed for %s", p.symbol)
        return BracketResult(ok=False, message=f"Bracket failed: {e}")


def place_trailing_stop(
    ib: IB,
    symbol: str,
    qty: int,
    trail_amount: float,
    tif: str = "DAY",
) -> BracketResult:
    """Place a standalone trailing-stop SELL order (no bracket parent).

    This is a backwards-compatible alias used by live_loop_10s to attach a
    trailing stop *after* a position is already open.
    """
    try:
        c = _contract(symbol)
        ib.qualifyContracts(c)
        trail_order = Order(
            action="SELL",
            orderType="TRAIL",
            totalQuantity=qty,
            auxPrice=trail_amount,
            tif=tif,
            transmit=True,
            eTradeOnly=False,
            firmQuoteOnly=False,
        )
        trade = ib.placeOrder(c, trail_order)
        ib.sleep(0.5)
        trail_id = trade.order.orderId
        return BracketResult(
            ok=True,
            message=f"Trailing stop placed for {symbol}",
            trail_id=trail_id,
        )
    except Exception as e:
        return BracketResult(ok=False, message=f"Trailing stop failed: {e}")
