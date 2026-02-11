import os
import sys

# Ensure project root is on sys.path so this script can be run directly
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.execution.orders import OrderRequest, place_order


def main():
    req = OrderRequest(symbol="SPY", side="BUY", quantity=1, order_type="MKT", stop_loss=480.0)
    result = place_order(req)
    print(result)


if __name__ == "__main__":
    main()
