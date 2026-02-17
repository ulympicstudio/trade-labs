# Corrected Bracket Order Structure

## New Architecture: Stop Loss + Trailing Stop

### Visual Structure
```
BUY NVDA @ $182.79
├─ STOP LOSS @ $180.71 (hard downside floor, -$2.08)
└─ TRAIL with $8.40 anchor (follows price up)
   
   OCA: Whichever fills first cancels the other
```

### Execution Scenarios

**Scenario 1: Stock drops immediately**
```
Entry: $182.79
   ↓ (price drops)
$180.71 → STOP LOSS fills (locked loss of $2.08 per share)
→ TRAIL order cancels (downside protected)
Result: Limited loss, predictable risk
```

**Scenario 2: Stock rises (the winning scenario)**
```
Entry: $182.79
   ↑ $185.00 (TRAIL now at $176.60, locked $8.40 profit floor)
   ↑ $190.00 (TRAIL now at $181.60, locked $8.40 profit floor)
   ↑ $195.00 (TRAIL now at $186.60, locked $8.40 profit floor)
   ↓ $187.80 (TRAIL hits @ $186.60, locked $3.81 gain)
→ TRAIL order fills (captured swing)
→ STOP LOSS order cancels (profitable exit)
Result: Profit locked in, swing captured
```

**Scenario 3: Stock stays flat**
```
Entry: $182.79
   ~ stays between $180.71 - $190.00
→ At end of day, order expires (DAY order)
Result: No fill, try again tomorrow
```

## Key Differences from Previous Structure

| Aspect | Old (Wrong) | New (Correct) |
|--------|-----------|--------------|
| **Child A** | Profit target @ $203.79 (too high, forces exit) | Stop loss @ $180.71 (downside protection) |
| **Child B** | TRAIL @ $8.40 | TRAIL @ $8.40 (same) |
| **OCA Logic** | Premature profit OR upside capture | Downside protection OR upside capture |
| **Use Case** | Limits gains, locks in early | Protects losses, captures full swing |

## Implementation Details

### BracketParams
```python
BracketParams(
    symbol="NVDA",
    qty=100,
    entry_limit=182.79,        # Buy price
    stop_loss=180.71,          # 2 * ATR down from entry
    trail_amount=8.40,         # 1.2 * ATR for upside tracking
    tif="DAY"
)
```

### Constants (from live_loop_10s.py)
- `STOP_LOSS_R = 2.0` — Risk floor: 2 ATRs below entry
- `TRAIL_ATR_MULT = 1.2` — Upside anchor: 1.2 ATRs trailing distance
- ATR is recalculated from 14-day bars for each stock

## When This Works Best
✅ Stocks with high volatility (big swings)
✅ Intraday trading (capture 2-3% moves)
✅ Low-slippage liquid symbols (NASDAQ cap-weights)
✅ When you want profit participation not profit limits

## Market Hours Behavior
- 9:30 AM ET: All orders become active
- Parent BUY enters queue
- If filled → TRAIL activates (follows price)
- If price drops → STOP LOSS fills first
- 4 PM ET: DAY orders expire if unfilled
