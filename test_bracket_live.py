#!/usr/bin/env python3
"""
Test corrected bracket order structure - SIM mode first
Shows what orders would be placed without executing
"""
import os
os.environ['TRADE_LABS_ARMED'] = '0'  # SIM mode - no actual orders
os.environ['TRADE_LABS_MODE'] = 'PAPER'
os.environ['TRADE_LABS_EXECUTION_BACKEND'] = 'SIM'

print("\n" + "="*70)
print("BRACKET ORDER TEST - Corrected Structure (SIM Mode)")
print("="*70 + "\n")

from src.signals.market_scanner import scan_us_most_active
from src.signals.score_candidates import score_candidates
from src.execution.bracket_orders import BracketParams, place_limit_tp_trail_bracket
from ib_insync import IB

# Get candidates
print("[SCAN] Fetching market candidates...")
candidates = scan_us_most_active(limit=30)
print(f"[SCAN] Found {len(candidates)} candidates\n")

if not candidates:
    print("[ERROR] No candidates found, cannot test")
    exit(1)

# Score candidates
print("[SCORE] Scoring candidates...")
scored = score_candidates(candidates, top_n=5)
print(f"[SCORE] Got {len(scored)} top candidates\n")

if not scored:
    print("[ERROR] No scored candidates, cannot test")
    exit(1)

# Show the bracket that would be placed
top_stock = scored[0]
symbol = top_stock['symbol']
px = top_stock['lastClose']
atr = top_stock['atr14']
momentum = top_stock['momentum60m']

print(f"[TEST] Top candidate: {symbol}")
print(f"  Price: ${px:.2f}")
print(f"  ATR14: ${atr:.2f}")
print(f"  Momentum: {momentum:.2%}\n")

# Calculate bracket (same logic as live_loop)
ENTRY_OFFSET_PCT = 0.0005
STOP_LOSS_R = 2.0
TRAIL_ATR_MULT = 1.2
RISK_PER_TRADE = 0.005  # 0.5%

entry = px * (1 - ENTRY_OFFSET_PCT)
stop_dist = atr  # Simple: ATR as our risk unit
stop_loss = entry - (STOP_LOSS_R * stop_dist)
trail_amt = atr * TRAIL_ATR_MULT

# Assume $1M account for qty calculation
account_equity = 1_000_000
risk_dollars = account_equity * RISK_PER_TRADE
qty = int(risk_dollars // stop_dist)

print(f"[BRACKET] Structure for {symbol}:")
print(f"\n  Entry (BUY LMT):           ${entry:.2f}")
print(f"  Stop Loss (SELL STP):      ${stop_loss:.2f}  (protection floor - TRUE stop order)")
print(f"  Trail Amount (TRAIL):      ${trail_amt:.2f}  (upside anchor)\n")

print(f"[MATH] Risk breakdown:")
print(f"  Risk per share:            ${entry - stop_loss:.2f}")
print(f"  Risk per trade (0.5%):     ${risk_dollars:,.0f}")
print(f"  Quantity:                  {qty} shares\n")

print(f"[SCENARIOS]")
scenarios = [
    (f"${px * 0.97:.2f}", "Stock drops 3%", "❌ STOP LOSS fills (limited loss)"),
    (f"${px * 1.02:.2f}", "Stock rises 2%", "✓ TRAIL follows up (gains locked)"),
    (f"${px * 1.05:.2f}", "Stock rises 5%", "✓ TRAIL follows up (gains growing)"),
    (f"${px * 1.08:.2f}", "Stock rises 8%", "✓ TRAIL follows up (max profit)"),
]

for price_str, scenario, outcome in scenarios:
    print(f"  {scenario:20} → Price {price_str:8} → {outcome}")

print(f"\n[STRUCTURE]")
print(f"  Parent:   BUY  {qty:4d} @ ${entry:.2f} (transmit=False)")
print(f"  Child A:  SELL {qty:4d} @ ${stop_loss:.2f} (STOP LOSS - OCA)")
print(f"  Child B:  SELL {qty:4d} Trail ${trail_amt:.2f} (TRAIL - OCA)")
print(f"  OCA Group: Both children in same group (mutual execution)")

print("\n" + "="*70)
print("This is SIM mode - no actual orders will be placed")
print("="*70 + "\n")

# Now ask user if they want to proceed with paper trading
response = input("Ready to test in PAPER TRADING mode (ARMED=0, IB connected)? [y/n]: ")

if response.lower() == 'y':
    print("\n[PAPER TEST] Testing with IB connection (no actual money at risk)...")
    print("[PAPER TEST] Starting 20-second test run...\n")
    
    # Run in paper mode with IB connection
    os.environ['IB_CLIENT_ID_OVERRIDE'] = '35'
    os.environ['TRADE_LABS_ARMED'] = '0'  # Still SIM, but with IB
    
    # Import and run live loop
    from src.live_loop_10s import main
    
    try:
        main()
    except KeyboardInterrupt:
        print("\n[TEST] Interrupted by user")
    except Exception as e:
        print(f"\n[ERROR] {e}")
else:
    print("[TEST] Skipped paper test")

print("\n✓ Test complete")
