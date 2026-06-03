"""
Quantitative Trading - Quick Start Guide
Run this to start quantitative swing trading with 100+ positions.
"""

from ib_insync import IB
from src.quant import (
    QuantMarketScanner,
    PortfolioRiskManager,
    run_quant_scan
)


# =============================================================================
# METHOD 1: SIMPLE (ONE-LINE SCAN)
# =============================================================================

def simple_scan():
    """Quick scan with default parameters."""
    ib = IB()
    ib.connect("127.0.0.1", 7497, clientId=1)
    
    # One function call does everything
    scores = run_quant_scan(
        ib,
        candidate_limit=100,      # Scan top 100 active stocks
        min_score=65.0,           # Minimum quant score
        min_confidence=55.0,      # Minimum confidence
        display_top_n=20          # Display top 20
    )
    
    ib.disconnect()
    return scores


# =============================================================================
# METHOD 2: FULL WORKFLOW (WITH RISK MANAGEMENT)
# =============================================================================

def full_workflow():
    """Complete workflow with portfolio filtering."""
    
    # Connect to IB
    ib = IB()
    ib.connect("127.0.0.1", 7497, clientId=1)
    
    # Step 1: Scan market
    print("\n" + "="*80)
    print("STEP 1: SCANNING MARKET")
    print("="*80)
    
    scanner = QuantMarketScanner(ib)
    
    scores = scanner.scan_and_score(
        candidate_limit=100,
        min_score=60.0,
        min_confidence=50.0
    )
    
    print(f"✓ Found {len(scores)} opportunities")
    scanner.display_top_opportunities(scores, top_n=20)
    
    # Step 2: Portfolio filtering
    print("\n" + "="*80)
    print("STEP 2: PORTFOLIO FILTERING")
    print("="*80)
    
    portfolio = PortfolioRiskManager(
        total_capital=100000,
        max_positions=50,
        max_risk_per_trade_pct=1.0,
        max_total_risk_pct=20.0
    )
    
    approved = portfolio.prioritize_opportunities(scores)
    
    print(f"\n✓ Approved {len(approved)} positions for execution")
    
    # Display portfolio status
    portfolio.display_portfolio_status()
    portfolio.display_open_positions(top_n=20)
    
    # Step 3: Execute trades (manual approval)
    print("\n" + "="*80)
    print("STEP 3: READY TO EXECUTE")
    print("="*80)
    
    print("\nTop 5 positions to execute:")
    print(f"{'Symbol':<8}{'Dir':<8}{'Qty':<8}{'Entry':<12}{'Stop':<12}{'Target':<12}{'Risk':<10}")
    print("-" * 80)
    
    for position in approved[:5]:
        print(f"{position['symbol']:<8}"
              f"{position['direction']:<8}"
              f"{position['quantity']:<8}"
              f"${position['entry_price']:<11.2f}"
              f"${position['stop_loss']:<11.2f}"
              f"${position['profit_target']:<11.2f}"
              f"${position['risk_amount']:<9.2f}")
    
    print("\nReady to place orders? See execute_trades() function below.")
    
    ib.disconnect()
    
    return approved


# =============================================================================
# METHOD 3: AUTO-EXECUTE (REQUIRES LIVE ACCOUNT)
# =============================================================================

def execute_trades(approved_positions, ib: IB):
    """
    Execute approved positions automatically.
    WARNING: This places real orders (paper-gated by TRADE_LABS_ARMED).

    Redirected to the canonical bracket module
    (``src.execution.bracket_orders.place_limit_tp_trail_bracket``): a LIMIT
    entry + OCA-linked STOP. There is NO take-profit limit leg — the trailing
    stop (attached after fill) is the profit-taker. Only LONG entries are
    supported via this helper; SHORT entries are skipped.

    NOTE: the previous implementation imported ``place_limit_order`` /
    ``place_stop_order`` which never existed in ``src.execution.orders`` —
    calling this function used to crash on import.
    """
    from src.execution.bracket_orders import (
        BracketParams,
        place_limit_tp_trail_bracket,
    )

    print("\n⚠️  EXECUTING ORDERS (paper-gated by TRADE_LABS_ARMED) ⚠️\n")

    executed_orders = []

    for position in approved_positions:
        symbol = position['symbol']
        direction = position['direction']
        quantity = position['quantity']

        if direction != "LONG":
            print(f"⊘ {symbol}: SHORT entries not supported by the bracket helper — skipped")
            continue

        try:
            params = BracketParams(
                symbol=symbol,
                qty=int(quantity),
                entry_limit=float(position['entry_price']),
                stop_loss=float(position['stop_loss']),
                trail_amount=0.0,  # trailing stop attaches after confirmed fill
                tif="DAY",
            )
            result = place_limit_tp_trail_bracket(ib, params)
            executed_orders.append({
                'symbol': symbol,
                'ok': result.ok,
                'parent_id': result.parent_id,
                'stop_id': result.stop_id,
                'degraded': result.degraded,
                'message': result.message,
            })
            print(f"{'✓' if result.ok else '✗'} {symbol}: {result.message}")
        except Exception as e:
            print(f"✗ {symbol}: Failed - {e}")

    return executed_orders


# =============================================================================
# EXAMPLE USAGE
# =============================================================================

if __name__ == "__main__":
    print("\n" + "="*80)
    print("QUANTITATIVE SWING TRADING - QUICK START")
    print("="*80)
    
    # Choose your method:
    
    # Method 1: Simple scan (recommended for testing)
    # scores = simple_scan()
    
    # Method 2: Full workflow (recommended for production)
    approved = full_workflow()
    
    # Method 3: Auto-execute (use with caution!)
    # ib = IB()
    # ib.connect("127.0.0.1", 7497, clientId=1)
    # executed = execute_trades(approved, ib)
    # ib.disconnect()
    
    print("\n" + "="*80)
    print("DONE!")
    print("="*80)
    print(f"\nNext steps:")
    print(f"1. Review approved positions above")
    print(f"2. Manually place orders in TWS/IB Gateway")
    print(f"3. Or use execute_trades() for auto-execution")
    print(f"\n" + "="*80 + "\n")


# =============================================================================
# ADVANCED: SCHEDULED SCANNING
# =============================================================================

def scheduled_scan_job():
    """
    Run this on a schedule (e.g., every 15 minutes during market hours).
    Can be integrated with src/main.py scheduler.
    """
    import logging
    from datetime import datetime
    
    logger = logging.getLogger(__name__)
    logger.info(f"Starting scheduled quant scan at {datetime.now()}")
    
    try:
        ib = IB()
        ib.connect("127.0.0.1", 7497, clientId=1)
        
        # Quick scan
        scores = run_quant_scan(
            ib,
            candidate_limit=100,
            min_score=70.0,        # Higher threshold for scheduled
            min_confidence=60.0,
            display_top_n=10
        )
        
        # Save to database
        from src.database.db_manager import TradeLabsDB
        db = TradeLabsDB()
        
        for score in scores[:10]:
            db.record_signal(
                run_id=f"quant_{datetime.now().strftime('%Y%m%d_%H%M')}",
                symbol=score.symbol,
                score=score.total_score,
                rank=scores.index(score) + 1,
                parameters={
                    'direction': score.direction,
                    'entry': score.suggested_entry,
                    'stop': score.suggested_stop,
                    'target': score.suggested_target,
                    'confidence': score.confidence
                }
            )
        
        ib.disconnect()
        logger.info(f"Scheduled scan complete: {len(scores)} opportunities")
        
    except Exception as e:
        logger.error(f"Scheduled scan failed: {e}", exc_info=True)


# =============================================================================
# TIPS & TRICKS
# =============================================================================

"""
CONFIGURATION TIPS:

1. AGGRESSIVE (More trades, higher risk):
   - min_score: 60.0
   - min_confidence: 50.0
   - max_risk_per_trade_pct: 2.0
   - max_total_risk_pct: 30.0

2. CONSERVATIVE (Fewer trades, lower risk):
   - min_score: 75.0
   - min_confidence: 70.0
   - max_risk_per_trade_pct: 0.5
   - max_total_risk_pct: 10.0

3. BALANCED (Default):
   - min_score: 65.0
   - min_confidence: 55.0
   - max_risk_per_trade_pct: 1.0
   - max_total_risk_pct: 20.0

SCANNING FREQUENCY:
- Every 15 minutes: High-frequency swing trading
- Every hour: Moderate frequency
- Once per day (9:45 AM ET): Low frequency

CAPITAL ALLOCATION:
- $10K - $50K: 20-30 positions
- $50K - $100K: 30-50 positions
- $100K - $500K: 50-100 positions
- $500K+: 100+ positions

MONITORING:
- Check positions every 15-30 minutes during market hours
- Update stop losses to trailing stops as price moves
- Close positions at end of day if needed (or carry overnight)
"""
