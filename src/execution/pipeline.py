from datetime import date
from typing import Optional

from ib_insync import IB

from src.contracts.trade_intent import TradeIntent
from src.risk.position_sizing import calculate_position_size
from src.risk.risk_guard import RiskState, approve_new_trade
from src.execution.orders import OrderRequest, place_order


def execute_trade_intent_paper(
    intent: TradeIntent,
    ib: IB,
    account_equity_usd: float,
    entry_price: float,
    open_risk_usd: float,
    atr: Optional[float] = None,
    atr_multiplier: float = 2.0,
    risk_percent: float = 0.005,
):
    # Determine stop
    stop_price = intent.stop_loss
    if stop_price is None:
        if atr is None:
            raise ValueError("No stop_loss and no ATR provided.")
        stop_price = entry_price - (atr * atr_multiplier)

    # Size shares if not provided
    if intent.quantity is None:
        sizing = calculate_position_size(
            account_equity=account_equity_usd,
            risk_percent=risk_percent,
            entry_price=entry_price,
            stop_price=stop_price,
            atr=None,
            atr_multiplier=atr_multiplier,
        )
        shares = sizing.shares
        proposed_trade_risk = sizing.total_risk
    else:
        shares = int(intent.quantity)
        proposed_trade_risk = shares * abs(entry_price - stop_price)

    # Risk gate
    state = RiskState(day=date.today())
    status = approve_new_trade(
        state=state,
        equity_usd=account_equity_usd,
        open_risk_usd=open_risk_usd,
        proposed_trade_risk_usd=proposed_trade_risk,
    )
    if not status.allowed:
        return {"ok": False, "reason": status.reason}

    # Place PAPER order (IB backend uses the SAME ib connection)
    req = OrderRequest(
        symbol=intent.symbol,
        side=intent.side,
        quantity=shares,
        order_type=intent.entry_type,
        stop_loss=stop_price,
    )
    result = place_order(req, ib=ib)

    return {"ok": True, "sized_shares": shares, "stop_price": stop_price, "order_result": result}
