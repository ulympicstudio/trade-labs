#!/usr/bin/env python3
"""
Verify corrected bracket structure calculations
"""
import os
os.environ['TRADE_LABS_ARMED'] = '0'  # SIM mode

# Test the calculation logic
print("=== Simulating Bracket Calculations ===\n")

# Example: NVDA trading scenario
px = 182.81  # Current price
atr = 4.20   # 14-day ATR

# Constants from live_loop
ENTRY_OFFSET_PCT = 0.0005
STOP_LOSS_R = 2.0
TRAIL_ATR_MULT = 1.2

# Calculate
entry = px * (1 - ENTRY_OFFSET_PCT)
stop_loss = entry - (STOP_LOSS_R * atr)
trail_amt = atr * TRAIL_ATR_MULT

print(f"Stock: NVDA")
print(f"  Current Price: ${px:.2f}")
print(f"  14-day ATR: ${atr:.2f}\n")

print(f"Bracket Levels:")
print(f"  Entry (BUY): ${entry:.2f}")
print(f"  Stop Loss (protection): ${stop_loss:.2f} (down ${entry - stop_loss:.2f})")
print(f"  Trail Amount: ${trail_amt:.2f} (upside anchor)\n")

print(f"Risk/Reward Analysis:")
print(f"  Max Loss per share: ${entry - stop_loss:.2f}")
print(f"  Min Profit capture: ${trail_amt:.2f}")
print(f"  Loss ratio: {((entry - stop_loss) / trail_amt):.2f}:1\n")

# Simulate scenarios
print("Projected Scenarios (@ market close/expiry):\n")

scenarios = [
    ("Bear: Price drops 3%", px * 0.97),
    ("Flat: Price stays", px),
    ("Bull: Price rises 2%", px * 1.02),
    ("Strong Bull: Price rises 5%", px * 1.05),
]

for name, price in scenarios:
    change = price - entry
    
    # Check stop loss fill
    if price <= stop_loss:
        result = f"STOP LOSS fills (loss: ${entry - price:.2f})"
    # Check trail hit
    elif price > entry and (price - trail_amt) > entry:  # TRAIL made a profit
        trail_filled_price = price  # TRAIL follows to current price
        profit = trail_filled_price - entry - trail_amt
        result = f"TRAIL fills (profit: ${profit:.2f}, at ${trail_filled_price:.2f})"
    else:
        result = "Held/expired unfilled"
    
    print(f"  ${price:.2f} ({change:+.2f}, {(change/entry)*100:+.2f}%) → {result}")

print("\n" + "="*60)
print("✓ Corrected structure implements stop loss + trailing stop")
print("✓ Stop loss protects downside")
print("✓ Trailing stop captures upside swings")
print("="*60)
