"""
Test the backtesting engine with a small sample.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import logging
from datetime import datetime, timedelta
from ib_insync import IB

from src.backtest import BacktestEngine

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


def test_backtest():
    """Test backtest with small sample."""
    
    print("\n" + "="*80)
    print("TEST BACKTESTING ENGINE")
    print("="*80 + "\n")
    
    # Connect to IB
    print("Connecting to IB...")
    ib = IB()
    
    try:
        ib.connect('127.0.0.1', 7497, clientId=5)
        print("✅ Connected\n")
    except Exception as e:
        print(f"❌ Failed to connect: {e}")
        return
    
    # Small test (last 30 days, 5 symbols)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)
    
    test_universe = ['AAPL', 'MSFT', 'GOOGL', 'TSLA', 'NVDA']
    
    print(f"Test Period: {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Test Universe: {test_universe}")
    print(f"Initial Capital: $10,000")
    print()
    
    # Initialize engine
    engine = BacktestEngine(
        ib=ib,
        start_date=start_date,
        end_date=end_date,
        initial_capital=10000.0,
        quant_weight=0.60,
        news_weight=0.40,
        max_positions=3,
        max_holding_days=10
    )
    
    # Run backtest
    result = engine.run_backtest(
        universe=test_universe,
        scan_frequency_days=2,
        min_unified_score=60.0,
        min_confidence=55.0
    )
    
    # Quick validation
    print("\n" + "="*80)
    print("TEST VALIDATION:")
    print("="*80)
    print(f"✅ Trades executed: {result.stats.total_trades}")
    print(f"✅ Win rate: {result.stats.win_rate:.1f}%")
    print(f"✅ Final equity: ${result.equity_curve['equity'].iloc[-1]:,.2f}" if not result.equity_curve.empty else "✅ No equity data")
    print(f"✅ Test passed\n")
    
    ib.disconnect()


if __name__ == '__main__':
    test_backtest()
