# 📋 TRADE LABS QUICK REFERENCE CARD
**Print this and keep it at your desk**

---

## 🚀 BASIC COMMANDS

### Navigate to Trade Labs
```bash
cd /Users/umronalkotob/trade-labs
```

### Check if everything is working
```bash
python preflight_check.py
```
✅ Should say: "ALL SYSTEMS GO"

### Run full market scan (60-90 seconds)
```bash
python run_hybrid_trading.py
```

### Quick morning routine (automated)
```bash
./morning_scan.sh
```

### Stop a running scan
Press: `Control + C`

---

## 📊 SPECIALIZED COMMANDS

### See what's trending (fast)
```bash
python test_news_integration.py
```

### Find earnings winners
```bash
python find_earnings.py
```

### Check specific stocks
```bash
python check_symbol.py
```
(Edit file first to add your symbols)

### Test on historical data (backtest)
```bash
python run_backtest.py
```
Takes 15-30 min, validates strategy

### Quick backtest (30 days)
```bash
python test_backtest.py
```
Takes 2-3 min

---

## 💰 EXECUTING TRADES (Arm/Disarm)

**Trade Labs scans, YOU execute in TWS**

### ARMED = Ready to Trade

1. **Run scan:** `python run_hybrid_trading.py`
2. **Review results:** Pick 5-10 best trades
3. **In TWS - Place Buy Order:**
   - Symbol: [From scan]
   - Action: BUY
   - Quantity: [Shares column]
   - Type: LIMIT
   - Price: [Entry column]
   - Click "Transmit"

4. **After fills - Set Stop:**
   - Right-click position → "Attach Order" → "Stop"
   - SELL at [Stop column] price, GTC

5. **After fills - Set Target (optional):**
   - Right-click position → "Attach Order" → "Limit"
   - SELL at [Target column] price, GTC

### DISARMED = Not Trading
- Don't run new scans
- Only manage existing positions
- Let stops/targets work automatically

**📖 Full Guide:** [EXECUTION_GUIDE.md](EXECUTION_GUIDE.md)

---

## 🎯 READING THE SCORES

| Score | Meaning | Action |
|-------|---------|--------|
| 75-100 | 🟢 Excellent | Take these trades |
| 65-74 | 🟡 Good | Solid opportunities |
| 50-64 | ⚪ Neutral | Be selective |
| 0-49 | 🔴 Weak | Skip these |

---

## 📈 SIGNAL TYPES

- **STRONG_BUY** → Take full position size
- **BUY** → Take normal position  
- **NEUTRAL** → Skip

---

## ⚙️ ADJUST SETTINGS

Open file:
```bash
open -a TextEdit run_hybrid_trading.py
```

Scroll to bottom and change these numbers:

### More Quality, Fewer Trades (Conservative)
```python
MIN_UNIFIED_SCORE = 70.0
MIN_CONFIDENCE = 70.0
MAX_POSITIONS = 30
```

### More Trades, Lower Quality (Aggressive)
```python
MIN_UNIFIED_SCORE = 60.0
MIN_CONFIDENCE = 55.0
MAX_POSITIONS = 75
```

### Balanced (Default)
```python
MIN_UNIFIED_SCORE = 65.0
MIN_CONFIDENCE = 60.0
MAX_POSITIONS = 50
```

---

## 🔧 BEFORE FIRST USE (ONE TIME ONLY)

### Set API key permanently
```bash
echo 'export FINNHUB_API_KEY="YOUR_FINNHUB_API_KEY"' >> ~/.zshrc
source ~/.zshrc
```

### Verify it worked
```bash
python test_finnhub_api.py
```
Should say: "✅ VALID AND WORKING"

---

## 🕐 RECOMMENDED SCHEDULE

| Time | Command | Purpose |
|------|---------|---------|
| 8:00 AM | `./morning_scan.sh` | Pre-market opportunities |
| 10:30 AM | `python run_hybrid_trading.py` | Post-open scan |
| 1:00 PM | `python run_hybrid_trading.py` | Mid-day check (optional) |

---

## 🆘 TROUBLESHOOTING

| Problem | Solution |
|---------|----------|
| Can't connect to IB | 1. Start TWS<br>2. Log in<br>3. Enable API (Configure → API → Settings) |
| No candidates found | 1. Normal on weekends<br>2. Lower MIN_NEWS_SCORE to 55 |
| Too many trades | Raise MIN_UNIFIED_SCORE to 70 |
| Too few trades | Lower MIN_UNIFIED_SCORE to 60 |
| System slow | Normal first run (60-90 sec) |
| API key not working | `export FINNHUB_API_KEY='...'` |

---

## 📁 WHERE FILES ARE SAVED

Scan results:
```
/Users/umronalkotob/trade-labs/hybrid_scan_YYYYMMDD_HHMMSS.json
```

Settings file:
```
/Users/umronalkotob/trade-labs/run_hybrid_trading.py
```

---

## 💡 PRO TIPS

1. **Run pre-market** - Best opportunities show up before open
2. **Re-scan at 10:30 AM** - After opening volatility settles
3. **Use ./morning_scan.sh** - Automates the whole routine
4. **Save different settings** - Make copies with conservative/aggressive settings
5. **Check earnings** - Run `find_earnings.py` weekly to see who's reporting

---

## 📞 HELP COMMANDS

```bash
# Full manual (you're here!)
open OPERATIONS_MANUAL.md

# Technical reference
open HYBRID_QUICK_REFERENCE.md

# News system details
open NEWS_SYSTEM_COMPLETE.md

# System status
open READY_TO_RUN.md
```

---

## 🎓 POSITION SIZING FORMULA

System risks **1% per trade**:

```
If account = $100,000:
  Risk per trade = $1,000
  
If entry = $100, stop = $95:
  Risk per share = $5
  Shares = $1,000 ÷ $5 = 200 shares
  Investment = 200 × $100 = $20,000
  
If stop hits: Lose $1,000 (1%)
If target hits (2.5:1): Gain $2,500 (2.5%)
```

---

## ⚡ EMERGENCY COMMANDS

Stop everything:
```bash
Control + C
```

Kill all Python processes:
```bash
killall python
```

Check if IB is connected:
```bash
python preflight_check.py
```

---

**Remember:** Trade Labs is a tool that helps you find trades. You review the results and decide what to execute in TWS.

**It's your assistant, not an autopilot.**

---

*Keep this card handy. Everything you need is here.*

