import logging
import math
import time
from dataclasses import dataclass
from typing import List, Optional

from ib_insync import IB, Stock, LimitOrder, Order, StopOrder

from config.runtime import is_paper, is_armed

log = logging.getLogger("bracket_orders")

# Order statuses that indicate IB has accepted a leg into its book.
_ACCEPTED_STATUSES = {"PreSubmitted", "Submitted", "Filled"}

# Fallback tick size when contract minTick is unavailable.
_DEFAULT_TICK = 0.01


@dataclass
class BracketParams:
    symbol: str
    qty: int
    entry_limit: float
    stop_loss: float  # Hard downside protection (STOP child)
    trail_amount: float  # Retained for API compatibility; trailing stop is
    # attached AFTER a confirmed fill via place_trailing_stop(), NOT at entry.
    tif: str = "DAY"

    @classmethod
    def from_plan(cls, plan) -> "BracketParams":
        """Build canonical bracket params from a bus ``OrderPlan``.

        The risk arm always emits ``entry_type="LMT"`` with a single
        ``limit_prices`` entry and a ``stop_price``; this is the one place
        that maps that intent onto the bracket builder so the OrderPlan and
        OrderBlueprint execution paths construct identical orders.
        """
        entry = float(plan.limit_prices[0]) if plan.limit_prices else float(plan.stop_price)
        trail_pct = float(plan.trail_params.get("trail_pct", 0.0) or 0.0)
        return cls(
            symbol=plan.symbol,
            qty=int(plan.qty),
            entry_limit=round(entry, 2),
            stop_loss=round(float(plan.stop_price), 2),
            trail_amount=round(entry * (trail_pct / 100.0), 2),
            tif=getattr(plan, "tif", "DAY") or "DAY",
        )

    @classmethod
    def from_blueprint(cls, bp) -> "BracketParams":
        """Build canonical bracket params from a premarket ``OrderBlueprint``.

        Entry is the mid of the entry ladder, matching the prior inline
        construction in execution_main._on_order_blueprint.
        """
        entry = bp.entry_ladder[len(bp.entry_ladder) // 2] if bp.entry_ladder else 0.0
        return cls(
            symbol=bp.symbol,
            qty=int(bp.qty),
            entry_limit=round(float(entry), 2),
            stop_loss=round(float(bp.stop_price), 2),
            trail_amount=round(float(entry) * (bp.trail_pct / 100.0), 2),
            tif="DAY",
        )


@dataclass
class BracketResult:
    """Canonical result of a bracket order submission.

    The entry bracket is intentionally TWO legs only:
        parent  – LIMIT BUY entry leg.
        stop    – STOP SELL hard downside protection (the genuinely last,
                  transmitted leg).

    There is NO take-profit limit leg. Upside is captured by a trailing
    stop attached after a confirmed fill (see place_trailing_stop()).
    """
    ok: bool
    message: str
    parent_id: Optional[int] = None
    stop_id: Optional[int] = None
    trail_id: Optional[int] = None
    degraded: bool = False


def _contract(symbol: str) -> Stock:
    return Stock(symbol, "SMART", "USD")


def _min_tick(ib: IB, contract) -> float:
    """Return the contract's minimum tick, falling back to 0.01."""
    try:
        details = ib.reqContractDetails(contract)
        if details:
            tick = float(getattr(details[0], "minTick", 0.0) or 0.0)
            if tick > 0:
                return tick
    except Exception as exc:  # pragma: no cover - defensive
        log.debug("reqContractDetails failed for %s: %s", contract.symbol, exc)
    return _DEFAULT_TICK


def _round_to_tick(price: float, tick: float) -> float:
    """Round a price to the nearest valid tick increment."""
    if tick <= 0:
        tick = _DEFAULT_TICK
    return round(round(price / tick) * tick, 10)


def _leg_accepted(trade) -> bool:
    """True if IB has acknowledged the leg (by status or trade log).

    We deliberately do NOT trust order.orderId, which ib_insync assigns
    locally on placeOrder() regardless of broker acceptance.
    """
    try:
        status = trade.orderStatus.status
    except Exception:
        status = None
    if status in _ACCEPTED_STATUSES:
        return True
    # Some accepted legs only surface via the trade log before status updates.
    try:
        for entry in trade.log:
            if getattr(entry, "status", None) in _ACCEPTED_STATUSES:
                return True
    except Exception:
        pass
    return False


def _cancel_legs(ib: IB, trades: List, symbol: str) -> None:
    """Cancel every already-placed leg (not just the parent)."""
    for tr in trades:
        if tr is None:
            continue
        try:
            ib.cancelOrder(tr.order)
        except Exception as exc:
            log.error("bracket_cancel_failed symbol=%s err=%s", symbol, exc)
    try:
        ib.sleep(0.3)
    except Exception:
        pass


def place_limit_tp_trail_bracket(
    ib: IB,
    p: BracketParams,
    oca_group: Optional[str] = None,
) -> BracketResult:
    """Place a TWO-leg entry bracket: LIMIT BUY parent + STOP SELL child.

    Design (owner decision):
      - Parent: BUY LMT at entry (transmit=False).
      - Child:  SELL STP at stop_loss, parentId set, OCA group with
                ocaType=1, transmit=True (the genuinely LAST leg).
      - NO take-profit limit leg.
      - The trailing stop is attached LATER, on confirmed fill, via
        place_trailing_stop(). It is NOT sent here against an unfilled parent.

    Acceptance is verified via trade.orderStatus.status / trade.log, not
    by reading the locally-assigned order.orderId. If a leg is not accepted,
    ALL already-placed legs are cancelled and the result is degraded.

    Refuses to run unless an explicit armed/paper flag is set.
    """
    # ── Armed / paper guard ──────────────────────────────────────────
    # Never silently place against a live-capable gateway.
    if not is_armed():
        return BracketResult(
            ok=False,
            message="BLOCKED: TRADE_LABS_ARMED=0. Set TRADE_LABS_ARMED=1 to place brackets.",
        )
    if not is_paper():
        return BracketResult(
            ok=False,
            message="BLOCKED: LIVE mode. Bracket placement refused (paper account only).",
        )

    placed: List = []
    try:
        c = _contract(p.symbol)
        ib.qualifyContracts(c)
        tick = _min_tick(ib, c)
        log.debug(
            "Contract qualified: %s secType=%s conId=%s minTick=%s",
            c.symbol, c.secType, c.conId, tick,
        )

        if oca_group is None:
            oca_group = f"OCA_{p.symbol}_{int(time.time())}"

        entry_px = _round_to_tick(p.entry_limit, tick)
        stop_px = _round_to_tick(p.stop_loss, tick)

        # ── Parent (entry) — not the last leg, do not transmit yet ──
        parent = LimitOrder("BUY", p.qty, entry_px)
        parent.tif = p.tif
        parent.transmit = False
        trade_parent = ib.placeOrder(c, parent)
        placed.append(trade_parent)
        ib.sleep(0.2)
        parent_id = parent.orderId
        log.debug("Parent BUY: id=%s qty=%s LMT=%.4f", parent_id, p.qty, entry_px)

        # ── Child: STOP (the last leg → transmit=True) ──
        stop_order = StopOrder("SELL", p.qty, stop_px)
        stop_order.parentId = parent_id
        stop_order.tif = p.tif
        stop_order.ocaGroup = oca_group
        stop_order.ocaType = 1  # CANCEL_WITH_BLOCK
        stop_order.transmit = True
        trade_stop = ib.placeOrder(c, stop_order)
        placed.append(trade_stop)
        ib.sleep(0.3)
        stop_id = stop_order.orderId
        log.debug(
            "STOP SELL: id=%s qty=%s STP=%.4f parentId=%s",
            stop_id, p.qty, stop_px, parent_id,
        )

        # ── Verify acceptance via broker ack, NOT local orderId ──
        bad = []
        if not _leg_accepted(trade_parent):
            bad.append("parent")
        if not _leg_accepted(trade_stop):
            bad.append("stop")

        if bad:
            log.warning(
                "bracket_degraded symbol=%s not_accepted=%s — cancelling all legs",
                p.symbol, bad,
            )
            _cancel_legs(ib, placed, p.symbol)
            return BracketResult(
                ok=False,
                message=f"DEGRADED: legs not accepted by IB: {bad}; all legs cancelled.",
                parent_id=parent_id,
                stop_id=stop_id,
                degraded=True,
            )

        return BracketResult(
            ok=True,
            message="2-leg bracket accepted (parent LMT + STOP); trail attaches on fill.",
            parent_id=parent_id,
            stop_id=stop_id,
            degraded=False,
        )
    except Exception as e:
        log.exception("Bracket placement failed for %s", p.symbol)
        # Best-effort cleanup of anything already placed.
        if placed:
            _cancel_legs(ib, placed, p.symbol)
        return BracketResult(ok=False, message=f"Bracket failed: {e}")


def place_trailing_stop(
    ib: IB,
    symbol: str,
    qty: int,
    trail_amount: float,
    tif: str = "DAY",
) -> BracketResult:
    """Place a standalone trailing-stop SELL order AFTER a position is open.

    This is the canonical upside-capture path: call it once the entry has a
    confirmed fill. The trailing amount is rounded to the contract minTick.

    Refuses to run unless an explicit armed/paper flag is set.
    """
    if not is_armed():
        return BracketResult(
            ok=False,
            message="BLOCKED: TRADE_LABS_ARMED=0. Set TRADE_LABS_ARMED=1 to place trailing stop.",
        )
    if not is_paper():
        return BracketResult(
            ok=False,
            message="BLOCKED: LIVE mode. Trailing stop refused (paper account only).",
        )
    try:
        c = _contract(symbol)
        ib.qualifyContracts(c)
        tick = _min_tick(ib, c)
        trail_aux = _round_to_tick(float(trail_amount), tick)
        if not math.isfinite(trail_aux) or trail_aux <= 0:
            return BracketResult(
                ok=False, message=f"Invalid trail amount {trail_amount}"
            )
        trail_order = Order(
            action="SELL",
            orderType="TRAIL",
            totalQuantity=qty,
            auxPrice=trail_aux,
            tif=tif,
            transmit=True,
        )
        trade = ib.placeOrder(c, trail_order)
        ib.sleep(0.3)
        if not _leg_accepted(trade):
            try:
                ib.cancelOrder(trade.order)
            except Exception:
                pass
            return BracketResult(
                ok=False,
                message=f"Trailing stop not accepted by IB for {symbol}",
            )
        return BracketResult(
            ok=True,
            message=f"Trailing stop placed for {symbol}",
            trail_id=trade.order.orderId,
        )
    except Exception as e:
        return BracketResult(ok=False, message=f"Trailing stop failed: {e}")
