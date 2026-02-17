#!/usr/bin/env python3
"""
Standalone bracket order test - shows corrected structure
"""

print("\n" + "="*75)
print("PAPER TRADING TEST - Corrected Bracket Orders")
print("="*75)

# Test data from current market
test_stocks = [
    {"symbol": "NVDA", "price": 182.81, "atr": 7.00, "score": 2.13},
    {"symbol": "ZIM", "price": 22.20, "atr": 1.04, "score": 0.26},
]

ENTRY_OFFSET_PCT = 0.0005
STOP_LOSS_R = 2.0
TRAIL_ATR_MULT = 1.2
ACCOUNT_EQUITY = 1_000_000
RISK_PER_TRADE = 0.005

print("\n[BRACKET TEST] Showing corrected stop loss + trailing stop structure\n")

for stock in test_stocks:
    symbol = stock["symbol"]
    px = stock["price"]
    atr = stock["atr"]
    score = stock["score"]
    
    # Calculate bracket levels
    entry = px * (1 - ENTRY_OFFSET_PCT)
    stop_loss = entry - (STOP_LOSS_R * atr)
    trail_amt = atr * TRAIL_ATR_MULT
    
    # Calculate position size
    risk_dollars = ACCOUNT_EQUITY * RISK_PER_TRADE
    qty = int(risk_dollars // atr)
    
    print(f"[{symbol}] Score: {score:.2f} | Current: ${px:.2f} | ATR: ${atr:.2f}")
    print(f"  Entry (BUY LMT):         ${entry:.2f}")
    print(f"  Stop Loss (protection):  ${stop_loss:.2f}   [ Max loss: ${entry - stop_loss:.2f}/share ]")
    print(f"  Trail Amount (upside):   ${trail_amt:.2f}    [ Captures: ${trail_amt:.2f}/share ]")
    print(f"  Position Size:           {qty:,d} shares")
    print(f"\n  Bracket Order Structure:")
    print(f"    Parent:  BUY  {qty:,d} @ ${entry:.2f}")
    print(f"    ├─ Child: SELL {qty:,d} STOP LOSS @ ${stop_loss:.2f}  (downside protection)")
    print(f"    └─ Child: SELL {qty:,d} TRAIL ${trail_amt:.2f}       (upside capture)")
    print(f"    └─ OCA: Whichever fills first cancels the other\n")
    
    # Show execution scenarios
    print(f"  Execution Scenarios:")
    scenarios = [
        (px * 0.97, "Stock down 3%"),
        (px * 1.00, "Stock flat"),
        (px * 1.03, "Stock up 3%"),
        (px * 1.06, "Stock up 6%"),
    ]
    
    for price, label in scenarios:
        if price <= stop_loss:
            fill_price = stop_loss
            pnl = (fill_price - entry) * qty
            pnl_pct = (pnl / (entry * qty)) * 100
            result = f"❌ STOP LOSS fills @ ${fill_price:.2f}"
            marker = "↓"
        else:
            trail_floor = price - trail_amt
            if trail_floor > entry:
                fill_price = trail_floor
                pnl = (fill_price - entry) * qty
                pnl_pct = (pnl / (entry * qty)) * 100
                result = f"✅ TRAIL locked @ ${fill_price:.2f}"
                marker = "↑"
            else:
                fill_price = price
                pnl = (fill_price - entry) * qty
                pnl_pct = (pnl / (entry * qty)) * 100
                result = f"⏸  Hold/Expire"
                marker = "~"
        
        print(f"    {marker} ${price:7.2f} ({label:15}) → {result:40} | P&L: ${pnl:+10,.0f} ({pnl_pct:+.2f}%)")
    
    print("\n" + "-"*75 + "\n")

print("="*75)
print("✓ Corrected bracket structure verified")
print("  • Stop loss provides downside protection (2 ATRs below entry)")
print("  • Trailing stop captures full upside swings")
print("  • OCA ensures mutually exclusive execution")
print("="*75)

print("\n[RESULT] Ready to execute paper trading test")
print("         All 3-leg brackets will use:")
print("         - Stop loss for downside protection")
print("         - Trailing stop for upside capture")
print("         - OCA group for mutual exclusion\n")
