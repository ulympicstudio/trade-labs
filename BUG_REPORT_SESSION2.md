# NEW BUGS DISCOVERED IN LIVE LOOP TEST
**Date:** February 16, 2026  
**Session:** Live loop testing with Finnhub + catalyst engine

---

## 🐛 BUG #5: Reddit Scraper Returning Invalid/Delisted Symbols

### Evidence
```
[REDDIT] Found 27 social buzz stocks
...
Error 200, reqId 437: No security definition has been found for the request, contract: Stock(symbol='LA', exchange='SMART', currency='USD')
Error 200, reqId 438: No security definition has been found for the request, contract: Stock(symbol='YAHOO', exchange='SMART', currency='USD')
...
```

### Invalid Symbols Found
- `LA` - Not a valid NYSE/NASDAQ symbol
- `YAHOO` - Not a valid ticker (never was; YHOO was the old one)
- `OK` - Not a valid stock symbol
- `Y` - Not a valid stock symbol
- `HDD` - Seagate/Western Digital? Delisted or wrong
- `BTC` - **CRYPTO** (not a stock!)
- `BACK`, `CCC`, `EMAT` - Junk

### Root Cause
Reddit scraper is picking up ANY ticker-like string from Reddit posts without validation:
```python
def hunt_reddit_mentions(self) -> Dict[str, CatalystStock]:
    # ...
    mentions = re.findall(r'\b([A-Z]{1,5})\b', text)  # Too permissive!
    # This regex matches ANY 1-5 letter uppercase string
    # Including: LA, YAHOO, BTC, CCC, OK, Y, BACK, etc.
```

### Impact
- **Severity:** HIGH - Wastes API calls, clogging catalyst list
- **Trades affected:** None (IB rejects them), but clutters signal discovery
- **Performance:** Many failed lookups = slower loop cycle

### Fix Required
1. **Whitelist validation**: Only accept known NYSE/NASDAQ symbols
2. **Real-time check**: Query IB contract validity before scoring
3. **Regex improvement**: Use more restrictive pattern + validate against ticker list

---

## 🐛 BUG #6: No Contract Validation Before Trading

### Evidence
```
Error 200, reqId 522: No security definition has been found for the request, contract: Stock(symbol='LA', exchange='SMART', currency='USD')
Error 200, reqId 523: No security definition has been found for the request, contract: Stock(symbol='YAHOO', exchange='SMART', currency='USD')
...
(repeated 30+ times)
```

### Root Cause
Live loop is trying to:
1. Hunt symbols (get 44 from catalyst engine)
2. Score symbols immediately
3. Try to trade without validating they exist in IB

**Should be:**
1. Hunt symbols
2. **Validate with IB** (requestContractDetails)
3. Score only valid ones
4. Trade valid ones

### Impact
- **Severity:** CRITICAL - Wasting IB API calls, slowing loop
- **Performance:** ~20 failed lookups per cycle = significant lag
- **User experience:** Spammy Error 200 messages

### Fix Required
Add validation step in live loop before scoring:
```python
def validate_contract(symbol):
    """Check if symbol exists with IB before attempting trade"""
    contract = Stock(symbol, "SMART", "USD")
    valid = ib.qualifyContracts(contract)
    return len(valid) > 0
```

---

## 🐛 BUG #7: Data Inconsistency Between Morning Report and Live Loop

### Evidence
**Morning Report Output:**
```
Total catalyst stocks: 44
Meet trading criteria (>70 score): 0       ← **0 CANDIDATES**
⚠️  No trading candidates identified
```

**But then Live Loop Found:**
```
[CATALYST] Found 10 high-quality opportunities
[CATALYST SCORED] 2 candidates ready           ← **2 CANDIDATES!**
```

### Root Cause
Two possible issues:

**Option A:** Different data being used
- Morning report uses one set of symbols
- Live loop uses a different set
- Likely: Live loop is filtering/scoring differently

**Option B:** Scoring threshold bypass
- Morning report applies score >70 threshold
- Live loop doesn't apply same threshold
- IRON, NVDA, ZIM might be scoring <70

### Impact
- **Severity:** HIGH - Inconsistent behavior, confusing for user
- **Trades:** Could execute trades that shouldn't meet criteria

### Evidence What Actually Scored
```
[SCORE] IRON score=1.27           ← Way below 70!
[SCORE] BTC score=-0.48           ← Negative!
[SCORE] NVDA score=2.13           ← Below 70!
[SCORE] ZIM score=0.26            ← Below 70!
```

Wait... why are these trading if they're <70?

---

## 🐛 BUG #8: Scoring System Seems Broken

### Evidence
```
[CATALYST] Found 10 high-quality opportunities      ← Claims "high-quality"
[CATALYST SCORED] 2 candidates ready

  [SCORE] IRON ✓ score=1.27
  [SCORE] NVDA ✓ score=2.13
  [SCORE] ZIM ✓ score=0.26
```

These scores are:
- **IRON:** 1.27 (should be >70 to trade!)
- **NVDA:** 2.13 (should be >70 to trade!)
- **ZIM:** 0.26 (should be >70 to trade!)

### Root Cause
The scoring output format changed. Looking at morning report:
```
LA: score=73.6 | signals=product
YAHOO: score=64.4 | signals=volume_spike
```

But live loop scoring shows:
```
IRON ✓ Momentum60m=-0.03% | ATR14=6.43 | LastClose=$55.95 | ADV20=$27.9M | score=1.27
```

**These are different scoring systems!**

1. Morning report: **Catalyst scoring** (0-100 scale, combines catalyst + technical)
2. Live loop: **Scanner scoring only** (shows only ATR/momentum, not catalyst boost)

### Impact
- **Severity:** CRITICAL - Two different scoring systems running!
- **Trades:** Live loop is trading on SCANNER score, not CATALYST score
- **Expected behavior:** Should be trading on CATALYST score (60% catalyst + 40% technical)

---

## 🐛 BUG #9: Exchange Filtering Insufficient

### Evidence
```
[REJECT] BTC: not tradeable (exchange=ARCA not allowed)
```

Good - BTC was rejected. But it shouldn't have made it to the scoring stage.

### Issues
1. BTC, BACK, CCC shouldn't be discovered at all (not stocks)
2. Invalid symbols (LA, YAHOO) shouldn't be scored
3. Only valid NYSE/NASDAQ symbols should pass catalyst hunter

### Root Cause
Catalyst hunter doesn't validate exchanges or symbols:
```python
def hunt_reddit_mentions(self):
    # Returns raw strings without validation
    # No check: "Is this a real stock?"
    # No check: "Is this NYSE/NASDAQ?"
    # No check: "Is this tradeable with IB?"
```

### Impact
- **Severity:** MEDIUM - Gets filtered eventually, but wastes resources

---

## 🐛 BUG #10: Scoring Output Mismatch

### Data Shown
```
[CATALYST] Found 10 high-quality opportunities         ← **Initial claim**
...
[CATALYST SCORED] 2 candidates ready                   ← **Final 2**
```

But which 10? And how did we get from 10 to 2?

### Root Cause
Not clear from logs if:
- 10 from catalyst engine, then scored down to 2?
- 10 invalid removed, 2-3 kept?
- Or just confused reporting?

### Impact
- **Severity:** LOW - Diagnostic/logging issue
- **User confusion:** Hard to understand what's happening

---

## Summary: Critical Issues

| Bug | Severity | Impact | Status |
|-----|----------|--------|--------|
| #5: Invalid symbols from Reddit | HIGH | Clutters discovery, wasted API calls | NEW 🔴 |
| #6: No contract validation | CRITICAL | 30+ failed IB lookups per cycle | NEW 🔴 |
| #7: Data inconsistency (morning vs live) | HIGH | Inconsistent behavior between reports | NEW 🔴 |
| #8: Scoring system changed in live loop | CRITICAL | Trading on wrong score threshold | NEW 🔴 |
| #9: Exchange filtering weak | MEDIUM | Invalid symbols make it too far | NEW 🔴 |
| #10: Scoring output unclear | LOW | Confusing logging | NEW 🔴 |

---

## Recommended Fixes (Priority Order)

### IMMEDIATE (Next 30 min)
1. **Fix #6**: Add IB contract validation before scoring
   - Check `ib.qualifyContracts()` before each lookup
   - Skip symbols that don't validate
   - Saves ~20-30 failed API calls per cycle

2. **Fix #8**: Verify same scoring system in both morning report and live loop
   - Morning report shows catalyst scores
   - Live loop shows different scores
   - Make them consistent (use CATALYST scores, not scanner)

### SHORT-TERM (Next hour)
3. **Fix #5**: Improve Reddit symbol extraction
   - Whitelist validation against known symbols
   - Filter out crypto (BTC, ETH, etc.)
   - More restrictive regex or symbol checking

4. **Fix #7**: Debug morning report vs live loop data source
   - Why morning report finds 0 trading candidates?
   - Why live loop finds 2-3 candidates?
   - Should be using same catalyst engine + scorer

### MEDIUM-TERM (Today)
5. **Fix #9**: Improve exchange filtering
   - Only NYSE/NASDAQ in discovery
   - Reject ARCA, PINK, OTC upfront
   - Add exchange validation to catalyst hunter

6. **Fix #10**: Clarify scoring output and logging
   - Log clearly: "Found 44, validated 10, scored 2"
   - Show why candidates were rejected
   - Better diagnostic output

---

## Questions for User

1. **Why are IRON, NVDA, ZIM being traded with scores of 1.27, 2.13, 0.26?**
   - These are WAY below the 70 threshold
   - Is live loop using a different threshold than morning report?

2. **Why is the morning report finding 0 candidates but live loop finding 2-3?**
   - Different catalyst sources?
   - Different scoring weights?
   - Different filtering?

3. **Should Reddit scraper include crypto (BTC, etc.) or invalid symbols (LA, YAHOO)?**
   - If no: Need to validate against symbol list
   - If yes: Need to understand why we'd want to trade crypto in stock account

4. **Are the "Error 200" messages normal?**
   - 30+ per cycle seems excessive
   - Suggests contracts should be validated before lookup

---

## Files to Review

- [src/data/catalyst_hunter.py](src/data/catalyst_hunter.py) - hunt_reddit_mentions() needs validation
- [src/live_loop_10s.py](src/live_loop_10s.py) - Scoring mismatch, contract validation
- [src/data/catalyst_scorer.py](src/data/catalyst_scorer.py) - Verify scoring formula consistency
- [src/data/research_engine.py](src/data/research_engine.py) - Morning report scoring

