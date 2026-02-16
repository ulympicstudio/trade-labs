"""
Quick test of trade history persistence.
"""

from src.utils.trade_history_db import TradeHistoryDB
from src.execution.orders import OrderResult
from datetime import datetime

# Initialize database
db = TradeHistoryDB("data/trade_history")

# Test recording a pipeline run
run_id = "test_run_001"
run = db.record_pipeline_run(
    run_id=run_id,
    backend="SIM",
    armed=False,
    num_candidates_scanned=5,
    num_candidates_executed=3,
    num_successful=3,
    details={"test": True}
)
print(f"✓ Recorded pipeline run: {run['run_id']}")

# Test recording a trade
order_result = OrderResult(
    ok=True,
    mode="PAPER",
    backend="SIM",
    armed=False,
    symbol="AAPL",
    side="BUY",
    quantity=100,
    order_type="MKT",
    stop_loss=145.00,
    timestamp=datetime.utcnow().isoformat(),
    message="Order placed",
    parent_order_id=123456,
    stop_order_id=123457,
)

trade = db.record_trade(
    run_id=run_id,
    symbol="AAPL",
    side="BUY",
    entry_price=150.25,
    quantity=100,
    stop_loss=145.00,
    order_result=order_result,
)
print(f"✓ Recorded trade: {trade['symbol']} {trade['quantity']} shares")

# Test closing a trade
closed = db.close_trade(order_id=123456, exit_price=152.50)
if closed:
    print(f"✓ Closed trade: P&L = ${closed['pnl']}, {closed['pnl_percent']:.2f}%")

# Get stats
stats = db.get_stats()
print(f"\n✓ Stats: {stats['total_trades']} trades, {stats['wins']} wins")

print("\nTrade history persistence working correctly!")
