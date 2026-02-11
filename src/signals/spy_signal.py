import os
import sys

# Add project root to sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from src.contracts.trade_intent import TradeIntent


def generate_intents():
    """
    MVP v0: always returns a single SPY intent.
    We use quantity=None and stop_loss=None so the execution pipeline sizes it
    and uses ATR fallback.
    """
    return [
        TradeIntent(
            symbol="SPY",
            side="BUY",
            entry_type="MKT",
            quantity=None,
            stop_loss=None,
            trailing_percent=None,
            rationale="MVP v0: hardcoded SPY to validate full pipeline"
        )
    ]
