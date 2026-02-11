from src.contracts.trade_intent import TradeIntent

intent = TradeIntent(
    symbol="SPY",
    side="BUY",
    entry_type="MKT",
    quantity=1,
    stop_loss=480.0,
    rationale="Execution spine test"
)

print(intent)
