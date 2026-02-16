#!/usr/bin/env python3
"""
Run Backtest for Trade Labs Hybrid System
Tests the system on historical data to validate performance.
"""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

import logging
from datetime import datetime, timedelta
from ib_insync import IB, util

from src.backtest import BacktestEngine

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def main():
    """Run backtest."""
    
    print("\n" + "="*80)
    print("TRADE LABS - BACKTESTING ENGINE")
    print("="*80 + "\n")
    
    # Connect to IB
    print("📊 Connecting to Interactive Brokers...")
    ib = IB()
    
    try:
        ib.connect('127.0.0.1', 7497, clientId=4)
        print("✅ Connected to IB TWS\n")
    except Exception as e:
        print(f"❌ Failed to connect to IB: {e}")
        print("   Make sure TWS/Gateway is running with API enabled")
        return
    
    # Backtest parameters
    print("⚙️  BACKTEST CONFIGURATION:")
    print("-" * 80)
    
    # Date range (last 6 months for faster testing)
    end_date = datetime.now()
    start_date = end_date - timedelta(days=180)  # 6 months
    
    print(f"Period:           {start_date.strftime('%Y-%m-%d')} to {end_date.strftime('%Y-%m-%d')}")
    print(f"Initial Capital:  $100,000")
    print(f"Max Positions:    30")
    print(f"Scan Frequency:   Every 3 days")
    print(f"Max Hold Time:    20 days")
    print(f"Risk per Trade:   1% of capital")
    print(f"Weights:          60% Quant, 40% News")
    print("-" * 80 + "\n")
    
    # Trading universe (common liquid stocks)
    universe = [
        # Tech
        'AAPL', 'MSFT', 'GOOGL', 'AMZN', 'NVDA', 'META', 'TSLA', 'AMD', 'NFLX', 'ADBE',
        # Finance
        'JPM', 'BAC', 'GS', 'MS', 'C', 'WFC',
        # Healthcare
        'UNH', 'JNJ', 'PFE', 'ABBV', 'LLY', 'MRK',
        # Consumer
        'WMT', 'HD', 'PG', 'KO', 'PEP', 'NKE', 'SBUX', 'MCD',
        # Energy
        'XOM', 'CVX', 'COP', 'SLB',
        # Industrial
        'BA', 'CAT', 'GE', 'UPS', 'HON',
        # Telecom
        'T', 'VZ', 'TMUS',
        # ETFs for diversification
        'SPY', 'QQQ', 'IWM', 'DIA'
    ]
    
    print(f"📈 Trading Universe: {len(universe)} symbols")
    print(f"   {', '.join(universe[:10])}...")
    print()
    
    # Initialize backtest engine
    print("🔧 Initializing backtest engine...")
    engine = BacktestEngine(
        ib=ib,
        start_date=start_date,
        end_date=end_date,
        initial_capital=100000.0,
        quant_weight=0.60,
        news_weight=0.40,
        max_positions=30,
        max_holding_days=20
    )
    print("✅ Engine initialized\n")
    
    # Run backtest
    print("🚀 RUNNING BACKTEST...")
    print("   This will take several minutes depending on data availability")
    print("   Progress will be shown below:\n")
    
    result = engine.run_backtest(
        universe=universe,
        scan_frequency_days=3,  # Scan every 3 days
        min_unified_score=65.0,
        min_confidence=60.0
    )
    
    # Results are printed by BacktestResult.print_summary()
    
    # Save detailed results
    print("\n💾 Saving detailed results...")
    
    # Save trades to CSV
    import pandas as pd
    
    if result.trades:
        trades_df = pd.DataFrame([
            {
                'symbol': t.symbol,
                'entry_date': t.entry_date,
                'entry_price': t.entry_price,
                'exit_date': t.exit_date,
                'exit_price': t.exit_price,
                'shares': t.shares,
                'pnl': t.pnl,
                'pnl_pct': t.pnl_pct,
                'exit_reason': t.exit_reason,
                'days_held': (t.exit_date - t.entry_date).days if t.exit_date else None
            }
            for t in result.trades
        ])
        
        trades_file = f"backtest_trades_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        trades_df.to_csv(trades_file, index=False)
        print(f"   ✅ Trades saved to: {trades_file}")
    
    # Save equity curve
    if not result.equity_curve.empty:
        equity_file = f"backtest_equity_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        result.equity_curve.to_csv(equity_file)
        print(f"   ✅ Equity curve saved to: {equity_file}")
    
    # Print winning trades
    print("\n🏆 TOP 10 WINNING TRADES:")
    print("-" * 80)
    winners = sorted([t for t in result.trades if t.pnl > 0],
                    key=lambda x: x.pnl, reverse=True)[:10]
    
    if winners:
        for i, trade in enumerate(winners, 1):
            print(f"{i:2d}. {trade.symbol:6s} | "
                  f"${trade.pnl:8,.2f} ({trade.pnl_pct:+6.2f}%) | "
                  f"{trade.entry_date.strftime('%Y-%m-%d')} → {trade.exit_date.strftime('%Y-%m-%d')} | "
                  f"{(trade.exit_date - trade.entry_date).days} days")
    else:
        print("   No winning trades")
    
    # Print losing trades
    print("\n📉 TOP 10 LOSING TRADES:")
    print("-" * 80)
    losers = sorted([t for t in result.trades if t.pnl < 0],
                   key=lambda x: x.pnl)[:10]
    
    if losers:
        for i, trade in enumerate(losers, 1):
            print(f"{i:2d}. {trade.symbol:6s} | "
                  f"${trade.pnl:8,.2f} ({trade.pnl_pct:+6.2f}%) | "
                  f"{trade.entry_date.strftime('%Y-%m-%d')} → {trade.exit_date.strftime('%Y-%m-%d')} | "
                  f"{trade.exit_reason}")
    else:
        print("   No losing trades")
    
    print("\n" + "="*80)
    print("BACKTEST COMPLETE")
    print("="*80)
    print("\n💡 Next Steps:")
    print("   1. Review the results above")
    print("   2. Check saved CSV files for detailed analysis")
    print("   3. Adjust parameters in run_backtest.py if needed")
    print("   4. Run again with different settings to optimize")
    print()
    
    # Disconnect
    ib.disconnect()


if __name__ == '__main__':
    main()
