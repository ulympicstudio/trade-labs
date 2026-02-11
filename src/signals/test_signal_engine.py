import os
import sys

# Add project root to sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from config.identity import SYSTEM_NAME, HUMAN_NAME
from src.broker.ib import IBBroker
from src.signals.signal_engine import get_trade_intents
from src.execution.pipeline import execute_trade_intent_paper


def main():
    print(f"\n{SYSTEM_NAME} → {HUMAN_NAME}: Running SPY MVP with REAL broker data + REAL account equity\n")

    # Initialize broker
    broker = IBBroker()
    broker.connect()

    # Generate intents
    intents = get_trade_intents()
    print(f"Intents returned: {len(intents)}\n")
    
    for i, intent in enumerate(intents, start=1):
        print(f"Intent #{i}")
        print(intent)

        # Fetch real data from broker
        entry_price = broker.get_last_price(intent.symbol)
        atr = broker.get_atr(intent.symbol)
        account_equity_usd = broker.get_account_equity()

        print(f"\nReal Entry Price: ${entry_price}")
        print(f"Real ATR: ${atr}")
        print(f"Real NetLiquidation: ${account_equity_usd:,.2f}")

        # Execute pipeline with real data
        result = execute_trade_intent_paper(
            intent=intent,
            account_equity_usd=account_equity_usd,
            entry_price=entry_price,
            open_risk_usd=0.0,
            atr=atr,
            atr_multiplier=2.0,
            risk_percent=0.005
        )

        pos = result["position_result"]
        order = result["order_result"]
        
        print(f"\nSized Shares: {pos['shares']}")
        print(f"Stop Price: ${pos['stop_price']}")
        print(f"Total Risk: ${pos['total_risk']:.2f}")
        print(f"\nPaper Order Result:")
        print(order)

    broker.disconnect()


if __name__ == "__main__":
    main()
