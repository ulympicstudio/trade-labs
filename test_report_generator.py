"""
Test report generator.
"""

from src.utils.trade_history_db import TradeHistoryDB
from src.utils.report_generator import ReportGenerator
from src.execution.orders import OrderResult
from datetime import datetime

# Initialize database and report generator
db = TradeHistoryDB("data/trade_history")
reporter = ReportGenerator(db)

# Record some test trades from today
today = datetime.utcnow().strftime("%Y-%m-%d")

# Create test order result
order_result = OrderResult(
    ok=True,
    mode="PAPER",
    backend="SIM",
    armed=False,
    symbol="TEST",
    side="BUY",
    quantity=100,
    order_type="MKT",
    stop_loss=0,
    timestamp=datetime.utcnow().isoformat(),
    message="Test",
    parent_order_id=999,
    stop_order_id=None,
)

# Record a trade
trade = db.record_trade(
    run_id="test_report",
    symbol="TSLA",
    side="BUY",
    entry_price=250.00,
    quantity=50,
    stop_loss=245.00,
    order_result=order_result,
)

# Close it with a profit
db.close_trade(order_id=999, exit_price=255.00)

# Generate and display report
report = reporter.generate_daily_report(today)
reporter.display_report(report)

# Save reports
reporter.save_report_markdown(report)
reporter.save_report_csv(report)

print(f"✓ Reports saved to data/reports/")
print(f"  - report_{today}.md")
print(f"  - report_{today}.csv")
