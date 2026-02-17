#!/usr/bin/env python3
"""
Direct test of 3-leg bracket order with TRAIL diagnostics
"""
import sys
import os

# Set up environment
os.environ.setdefault('TRADE_LABS_ARMED', '1')
os.environ.setdefault('TRADE_LABS_MODE', 'PAPER')
os.environ.setdefault('TRADE_LABS_EXECUTION_BACKEND', 'IB')
os.environ.setdefault('IB_CLIENT_ID_OVERRIDE', '30')

from ib_insync import IB
from src.execution.bracket_orders import place_limit_tp_trail_bracket, BracketParams

def test_trail_bracket():
    """Test 3-leg bracket with TRAIL diagnostics"""
    print("\n=== TRAIL Bracket Diagnostic Test ===\n")
    
    # Connect to IB
    print("[CONNECT] Connecting to Interactive Brokers...")
    ib = IB()
    try:
        ib.connect('127.0.0.1', 7497, clientId=30, timeout=10)
        print(f"[CONNECT] Connected: {ib.isConnected()}")
    except Exception as e:
        print(f"[ERROR] Connection failed: {e}")
        return
    
    # Test with NVDA
    print(f"\n[TEST] Creating 3-leg bracket for NVDA...")
    print(f"  Structure: BUY @ entry → STOP LOSS (downside) + TRAIL (upside in OCA)")
    params = BracketParams(
        symbol="NVDA",
        qty=100,
        entry_limit=182.79,
        stop_loss=180.71,  # Hard downside: entry - 2*ATR
        trail_amount=8.40,  # Upside: follows price up
        tif="DAY"
    )
    
    result = place_limit_tp_trail_bracket(ib, params, oca_group="TEST_OCA_001")
    
    print(f"\n[RESULT] Bracket result:")
    print(f"  ok: {result.ok}")
    print(f"  message: {result.message}")
    print(f"  parent_id: {result.parent_id}")
    print(f"  tp_id: {result.tp_id}")
    print(f"  trail_id: {result.trail_id}")
    
    # Check TWS
    print(f"\n[TWS] Please check TWS for:")
    print(f"  - Parent order (BUY LMT)")
    print(f"  - TP SELL order")
    print(f"  - TRAIL SELL order (THIS IS MISSING)")
    
    # Keep connection open to see orders
    ib.sleep(5)
    
    # Disconnect
    print(f"\n[DISCONNECT] Closing connection...")
    ib.disconnect()
    print("[DONE]")

if __name__ == "__main__":
    test_trail_bracket()
