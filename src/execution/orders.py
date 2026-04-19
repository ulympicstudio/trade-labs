import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from ib_insync import IB, Stock, MarketOrder, StopOrder

from config.runtime import is_paper, execution_backend, is_armed
from src.execution.bracket_orders import (
    BracketParams,
    place_limit_tp_trail_bracket,
)

log = logging.getLogger("orders")

FINGERPRINT = "ORDERS_SAFE_GATE_V1"


@dataclass
class OrderRequest:
    symbol: str
    side: str          # "BUY" or "SELL"
    quantity: int
    order_type: str    # "MKT" (MVP)
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

    # Hard block LIVE forever for now
    if not is_paper():
        return OrderResult(
            ok=False, mode=mode, backend=backend, armed=armed,
            symbol=req.symbol, side=req.side, quantity=req.quantity,
            order_type=req.order_type, stop_loss=req.stop_loss,
            timestamp=ts,
            message="LIVE mode blocked (not enabled)."
        )

    # SAFE DEFAULT: SIM always allowed, never hits broker
    if backend == "SIM":
        return OrderResult(
            ok=True, mode=mode, backend=backend, armed=armed,
            symbol=req.symbol, side=req.side, quantity=req.quantity,
            order_type=req.order_type, stop_loss=req.stop_loss,
            timestamp=ts,
            message="SIM order accepted (no broker submission)."
        )

    # IB backend requires ARMED=1
    if backend == "IB" and not armed:
        return OrderResult(
            ok=False, mode=mode, backend=backend, armed=armed,
            symbol=req.symbol, side=req.side, quantity=req.quantity,
            order_type=req.order_type, stop_loss=req.stop_loss,
            timestamp=ts,
            message="BLOCKED: TRADE_LABS_ARMED=0. Set TRADE_LABS_ARMED=1 to allow IB paper orders."
        )

    if backend != "IB":
        return OrderResult(
            ok=False, mode=mode, backend=backend, armed=armed,
            symbol=req.symbol, side=req.side, quantity=req.quantity,
            order_type=req.order_type, stop_loss=req.stop_loss,
            timestamp=ts,
            message=f"Unknown backend: {backend}"
        )

    if ib is None:
        return OrderResult(
            ok=False, mode=mode, backend=backend, armed=armed,
            symbol=req.symbol, side=req.side, quantity=req.quantity,
            order_type=req.order_type, stop_loss=req.stop_loss,
            timestamp=ts,
            message="IB backend requires an active IB connection passed in."
        )

    # ---- REAL IB PAPER ORDER SUBMISSION (via bracket_orders) ----
    try:
        entry_limit = _compute_limit_price(ib, req.symbol)
    except ValueError as e:
        return OrderResult(
            ok=False, mode=mode, backend=backend, armed=armed,
            symbol=req.symbol, side=req.side, quantity=req.quantity,
            order_type=req.order_type, stop_loss=req.stop_loss,
            timestamp=ts,
            message=f"Cannot compute entry price: {e}"
        )

    stop_price = req.stop_loss
    if stop_price is None:
        try:
            stop_price = _compute_stop(ib, req.symbol)
        except ValueError as e:
            return OrderResult(
                ok=False, mode=mode, backend=backend, armed=armed,
                symbol=req.symbol, side=req.side, quantity=req.quantity,
                order_type=req.order_type, stop_loss=req.stop_loss,
                timestamp=ts,
                message=f"Cannot compute stop: {e}"
            )

    params = BracketParams(
        symbol=req.symbol,
        qty=int(req.quantity),
        entry_limit=entry_limit,
        stop_loss=stop_price,
        trail_amount=0.0,  # trail added after fill confirmation
        tif="DAY",
    )
    result = place_limit_tp_trail_bracket(ib, params)

    return OrderResult(
        ok=result.ok, mode=mode, backend=backend, armed=armed,
        symbol=req.symbol, side=req.side, quantity=req.quantity,
        order_type=req.order_type, stop_loss=req.stop_loss,
        timestamp=ts,
        message=result.message,
        parent_order_id=result.parent_id,
        stop_order_id=result.stop_id,
    )


def _compute_limit_price(
    ib: IB,
    symbol: str,
    offset_pct: float = 0.001,
    fallback_price: float | None = None,
) -> float:
    """Compute aggressive limit entry price with robust fallback chain.

    Tries in order:
      1. reqMktData ask price
      2. reqHistoricalData last bar close
      3. caller-supplied fallback_price
      4. raises ValueError if all exhausted
    """
    c = Stock(symbol, "SMART", "USD")

    # ── Attempt 1: live ask via market data ──
    try:
        ticker = ib.reqMktData(c, "", False, False)
        ib.sleep(0.5)
        ask = ticker.ask if ticker.ask and ticker.ask > 0 else None
        last = ticker.last if ticker.last and ticker.last > 0 else None
        ib.cancelMktData(c)
        price = ask or last
        if price and price > 0:
            return round(price * (1 + offset_pct), 2)
    except Exception as exc:
        log.warning("_compute_limit_price reqMktData failed for %s: %s", symbol, exc)

    # ── Attempt 2: historical bar close ──
    try:
        bars = ib.reqHistoricalData(c, "", "1 D", "1 min", "TRADES", False)
        if bars:
            close = bars[-1].close
            if close and close > 0:
                log.info("_compute_limit_price using historical close for %s: %.2f", symbol, close)
                return round(close * (1 + offset_pct), 2)
    except Exception as exc:
        log.warning("_compute_limit_price reqHistoricalData failed for %s: %s", symbol, exc)

    # ── Attempt 3: caller-supplied fallback ──
    if fallback_price is not None and fallback_price > 0:
        log.info("_compute_limit_price using fallback_price for %s: %.2f", symbol, fallback_price)
        return round(fallback_price * (1 + offset_pct), 2)

    raise ValueError(f"Cannot get price for {symbol}: ask, historical, and fallback all exhausted")


def _compute_stop(ib: IB, symbol: str, atr_mult: float = 1.5) -> float:
    """Compute ATR-based stop price from recent bars."""
    c = Stock(symbol, "SMART", "USD")
    bars = ib.reqHistoricalData(c, "", "5 D", "1 day", "TRADES", False)
    if not bars or len(bars) < 2:
        raise ValueError(f"Insufficient bar data for stop on {symbol}")
    ranges = [b.high - b.low for b in bars[-5:]]
    atr = sum(ranges) / len(ranges)
    last_close = bars[-1].close
    return round(last_close - atr * atr_mult, 2)