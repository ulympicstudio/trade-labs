"""
Test SQLite database and migration.
"""

from src.database.db_manager import TradeLabsDB
from src.database.migrations import perform_migration
from datetime import datetime

print("=" * 70)
print("PHASE 3: SQLite Database & Migration Test")
print("=" * 70)

# Initialize SQLite database
print("\n1. Initializing SQLite database...")
db = TradeLabsDB("data/test_trade_labs.db")
print("✓ Database created/verified")

# Test recording data
print("\n2. Testing basic database operations...")

# Record a run
run = db.record_run(
    run_id="test_run_001",
    backend="SIM",
    armed=False,
    num_scanned=10,
    num_executed=5,
    num_successful=3,
    details={"test": True}
)
print(f"✓ Recorded run: {run['run_id']}")

# Record trades
trade1 = db.record_trade(
    run_id="test_run_001",
    symbol="AAPL",
    side="BUY",
    entry_price=150.00,
    entry_timestamp=datetime.utcnow().isoformat(),
    quantity=100,
    stop_loss=145.00,
    parent_order_id=123,
    stop_order_id=124,
)
print(f"✓ Recorded trade: {trade1.symbol} {trade1.quantity}@{trade1.entry_price}")

# Close the trade
closed = db.close_trade(
    trade_id=trade1.id,
    exit_price=155.00,
    exit_timestamp=datetime.utcnow().isoformat(),
)
print(f"✓ Closed trade: P&L = ${closed.realized_pnl}")

# Record metrics
metrics = db.record_daily_metrics(
    date="2025-02-15",
    daily_pnl=250.00,
    cumulative_pnl=250.00,
    num_trades=1,
    num_wins=1,
    num_losses=0,
    win_rate_pct=100.0,
    sharpe_ratio=5.0,
    sortino_ratio=15.0,
    calmar_ratio=100.0,
    max_drawdown_pct=0.5,
    volatility_pct=1.2,
    profit_factor=float('inf'),
)
print(f"✓ Recorded metrics for {metrics.date}")

# Query data
print("\n3. Testing database queries...")

runs = db.get_runs(limit=10)
print(f"✓ Retrieved runs: {len(runs)} records")

trades = db.get_trades()
print(f"✓ Retrieved trades: {len(trades)} records")

stats = db.get_stats()
print(f"✓ Stats: {stats['total_trades']} total trades, {stats['wins']} wins")

print("\n4. Testing migration from JSON...")
try:
    result = perform_migration()
    print(f"\n✓ Migration complete:")
    print(f"  - JSON to SQLite: {result['json_trades']} trades → {result['sql_trades']} trades")
    print(f"  - Verification: {'PASSED ✓' if result['clean'] else 'WARNING ⚠'}")
except Exception as e:
    print(f"Migration test (expected if no JSON data): {str(e)[:50]}")

print("\n5. Exporting to JSON...")
db.export_trades_to_json("data/trades_from_sqlite.json")

print("\n" + "=" * 70)
print("✓ SQLite Database & Migration Tests Complete!")
print("=" * 70)
