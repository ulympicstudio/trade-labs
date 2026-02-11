import os
import sys

# Add project root to sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.signals.signal_engine import get_trade_intents
from src.execution.pipeline import execute_trade_intent_paper


def main():
    print(f"{SYSTEM_NAME} → {HUMAN_NAME}: generating MVP v0 trade intents...")

    intents = get_trade_intents()
    print(f"Intents returned: {len(intents)}")
    for i, intent in enumerate(intents, start=1):
        print(f"\nIntent #{i}")
        print(intent)

        # Feed into your existing execution pipeline (paper)
        # Using placeholder values for now:
        result = execute_trade_intent_paper(
            intent=intent,
            account_equity_usd=100000,   # placeholder equity for MVP test
            entry_price=500.0,           # placeholder entry
            open_risk_usd=0.0,
            atr=5.0,                     # placeholder ATR
            atr_multiplier=2.0,
            risk_percent=0.005
        )
        print("\nPipeline result:")
        print(result)


if __name__ == "__main__":
    main()
