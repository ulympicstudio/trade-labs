import os
import sys

# Ensure project root is on sys.path so this script can be run directly
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.orders import OrderRequest, place_order
from src.risk.position_sizing import calculate_position_size


def main():
    # Example sizing parameters
    account_equity = 100_000
    risk_percent = 0.005
    entry_price = 500.0
    atr = 5.0
    atr_multiplier = 2.0

    # Calculate position size (derives stop from ATR)
    pos = calculate_position_size(
        account_equity=account_equity,
        risk_percent=risk_percent,
        entry_price=entry_price,
        atr=atr,
        atr_multiplier=atr_multiplier,
    )

    # Build order using sized shares and derived stop
    req = OrderRequest(
        symbol="SPY",
        side="BUY",
        quantity=pos.shares,
        order_type="MKT",
        stop_loss=pos.stop_price,
    )

    result = place_order(req)

    # Print requested output
    print(f"ok: {result.ok}")
    print(f"sized_shares: {pos.shares}")
    print(f"stop_price: {pos.stop_price}")
    print(result)


if __name__ == "__main__":
    main()
