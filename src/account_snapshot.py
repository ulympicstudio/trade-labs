"""Capture a timestamped snapshot of account state from Interactive Brokers."""
import os
import sys
from datetime import datetime

# Ensure project root is on sys.path
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from ib_insync import IB
from config.ib_config import IB_HOST, IB_PORT, IB_CLIENT_ID

ib = IB()
ib.connect(IB_HOST, IB_PORT, clientId=IB_CLIENT_ID)

acct = ib.managedAccounts()[0]
summary = ib.accountSummary(acct)

# Snapshot with timestamp
ts = datetime.now().isoformat()
print(f"Account Snapshot: {ts}")
print(f"Account: {acct}\n")

# Show key metrics
keep = {
    "NetLiquidation",
    "AvailableFunds",
    "BuyingPower",
    "CashBalance",
    "EquityWithLoanValue",
    "Equity",
    "TotalCashValue",
    "UnrealizedPnL",
    "RealizedPnL",
}
for s in summary:
    if s.tag in keep and s.currency == "USD":
        print(f"{s.tag:25} {s.value:>15} {s.currency}")

ib.disconnect()
