"""Execution pipeline for trade intents."""
import os
import sys

# Add project root to sys.path for imports
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.contracts.trade_intent import TradeIntent
from src.execution.orders import OrderRequest, place_order
from src.risk.position_sizing import calculate_position_size


def execute_trade_intent_paper(
    intent: TradeIntent,
    account_equity_usd: float,
    entry_price: float,
    open_risk_usd: float,
    atr: float,
    atr_multiplier: float,
    risk_percent: float,
) -> dict:
    """
    Execute a trade intent in paper mode.

    1. Calculate position size using ATR and risk parameters
    2. Create an order request with sized quantity and stop loss
    3. Place the order in PAPER mode
    4. Return the order result

    Parameters:
        intent: The TradeIntent to execute
        account_equity_usd: Total account equity
        entry_price: Entry price for the trade
        open_risk_usd: Current open risk (for portfolio checks)
        atr: Average True Range for stop distance
        atr_multiplier: Multiplier for ATR (e.g., 2.0 for 2x ATR stop)
        risk_percent: Risk as percentage of equity (e.g., 0.005 for 0.5%)

    Returns:
        A dict with 'position_result' and 'order_result' keys
    """
    # Calculate position sizing
    pos = calculate_position_size(
        account_equity=account_equity_usd,
        risk_percent=risk_percent,
        entry_price=entry_price,
        atr=atr,
        atr_multiplier=atr_multiplier,
    )

    # Create order request using intent symbol and side, with sized quantity and stop
    order_req = OrderRequest(
        symbol=intent.symbol,
        side=intent.side,
        quantity=pos.shares,
        order_type=intent.entry_type,
        stop_loss=pos.stop_price,
    )

    # Place order in PAPER mode (default)
    order_result = place_order(order_req, mode="PAPER")

    return {
        "position_result": {
            "entry_price": pos.entry_price,
            "stop_price": pos.stop_price,
            "shares": pos.shares,
            "total_risk": pos.total_risk,
        },
        "order_result": order_result,
    }
