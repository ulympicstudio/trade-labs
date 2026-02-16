# Finnhub API Key Setup - COMPLETE ✅

## Status: Your API Key is Working!

**API Key:** `YOUR_FINNHUB_API_KEY`

**Test Results:**
- ✅ API connection successful
- ✅ Fetched 1,248 upcoming earnings events
- ✅ Retrieved AAPL earnings history (75% beat rate, +3.33% avg surprise)
- ✅ All earnings features now enabled

---

## What You Have Now

### Earnings Features Activated:
1. **Earnings Calendar** - 1,248 upcoming reports in next 14 days
2. **Historical Beat Rates** - Track which companies consistently beat estimates
3. **Earnings Statistics** - Average surprise %, beat rate %, recent performance
4. **High-Probability Plays** - Identify stocks with 70%+ beat rates before earnings
5. **Catalyst Scoring** - Enhanced news scoring with earnings data

### Example: AAPL Earnings Data
- Beat Rate: 75% (3 out of 4 quarters beat estimates)
- Avg Surprise: +3.33%
- Last 4 Quarters: 75% beat rate
- System will boost AAPL's score near earnings dates

---

## Current Session (Active)

Your API key is set for this terminal session:
```bash
export FINNHUB_API_KEY='YOUR_FINNHUB_API_KEY'
```

**Status:** ✅ Active in current terminal

**Works for:**
- This terminal session only
- Any Python scripts run from this terminal
- Hybrid trading system started from here

**Expires:** When you close this terminal window

---

## Make It Permanent (Recommended)

To have the API key available in all future terminal sessions:

### Option 1: Add to ~/.zshrc (Recommended)
```bash
echo 'export FINNHUB_API_KEY="YOUR_FINNHUB_API_KEY"' >> ~/.zshrc
source ~/.zshrc
```

**After running this:**
- ✅ API key available in all new terminals
- ✅ Persists across reboots
- ✅ Automatically loaded on startup

### Option 2: Use .env File (Already Created)
The API key is saved in `.env` file (already done):
```
/Users/umronalkotob/trade-labs/.env
```

**To use:**
```python
from dotenv import load_dotenv
load_dotenv()  # Loads FINNHUB_API_KEY from .env
```

**Note:** Requires `python-dotenv` package:
```bash
pip install python-dotenv
```

---

## Using Earnings Features

### 1. Hybrid Trading System (Automatic)
```python
from run_hybrid_trading import HybridTradingSystem

# API key automatically read from environment
hybrid = HybridTradingSystem(
    ib_connection=ib,
    quant_weight=0.60,
    news_weight=0.40
)

# Earnings data included in news scoring
positions = hybrid.run_full_scan()
```

**What happens:**
- News scorer checks upcoming earnings (next 14 days)
- Boosts score for stocks with 70%+ historical beat rate
- Identifies "consistent beaters" (high-probability plays)
- Adds +10-20 points to news score if earnings imminent + strong history

### 2. Find Earnings Winners
```python
from src.data.news_scorer import NewsScorer
import os

scorer = NewsScorer(earnings_api_key=os.environ.get('FINNHUB_API_KEY'))

# Get stocks with upcoming earnings + strong beat rates
winners = scorer.get_earnings_winners(
    days_ahead=14,       # Next 2 weeks
    min_beat_rate=70.0   # 70%+ historical beat rate
)

for winner in winners[:10]:
    print(f"{winner['symbol']}: "
          f"{winner['beat_rate']:.0f}% beat rate, "
          f"{winner['days_until']} days until earnings")
```

**Sample Output:**
```
AAPL: 75% beat rate, 5 days until earnings
MSFT: 80% beat rate, 8 days until earnings
NVDA: 88% beat rate, 12 days until earnings
```

### 3. Check Specific Stock Earnings
```python
from src.data.earnings_calendar import EarningsCalendar
import os

calendar = EarningsCalendar(api_key=os.environ.get('FINNHUB_API_KEY'))

# Get statistics for any symbol
stats = calendar.calculate_earnings_statistics('TSLA')

print(f"Beat Rate: {stats['beat_rate']:.0f}%")
print(f"Avg Surprise: {stats['avg_surprise_pct']:+.2f}%")
print(f"Last 4 Quarters: {stats['last_4_beat_rate']:.0f}%")
```

---

## API Rate Limits

**Finnhub Free Tier:**
- 60 API calls per minute
- 30 API calls per second

**System Usage:**
- Hybrid scan: ~5-10 API calls per run
- News scoring: ~1 call per symbol
- **You have plenty of headroom** ✅

**If you exceed limits:**
- System gracefully falls back to Google News RSS
- Earnings features temporarily disabled
- Wait 60 seconds and try again

---

## Verify API Key Anytime

### Quick Test:
```bash
export FINNHUB_API_KEY='YOUR_FINNHUB_API_KEY'
python test_finnhub_api.py
```

### Check in Pre-Flight:
```bash
export FINNHUB_API_KEY='YOUR_FINNHUB_API_KEY'
python preflight_check.py
```

Should show:
```
✅ FINNHUB_API_KEY set (d68l5d1r...5rjfun3g)
   Enables: Earnings calendar, professional news
```

---

## Enhanced News Scoring

**Before API Key:**
- Sentiment: 35%
- Catalyst: 30%
- Buzz: 15%
- Earnings: 20% ❌ (disabled)

**With API Key:**
- Sentiment: 35%
- Catalyst: 30%
- Buzz: 15%
- Earnings: 20% ✅ (active)

**Earnings Score Formula:**
```
if upcoming_earnings within 30 days:
  base_score = historical_beat_rate
  
  if beat_rate >= 75%:
    score = 80 + boost
  elif beat_rate >= 60%:
    score = 60 + (beat_rate - 60)
  else:
    score = beat_rate
  
  if earnings within 7 days:
    score += 10  # Urgency boost
```

---

## Files Created/Updated

1. ✅ `.env` - API key stored (not committed to git)
2. ✅ `test_finnhub_api.py` - Test script
3. ✅ `run_hybrid_trading.py` - Updated to use API key automatically
4. ✅ `.gitignore` - Already excludes .env (safe)

---

## Security Notes

✅ **Your API key is safe:**
- Stored in `.env` file
- `.env` is in `.gitignore` (won't be committed to git)
- Only visible in your terminal environment
- Not exposed in code repositories

⚠️ **Don't:**
- Commit `.env` to git
- Share your API key publicly
- Hardcode it in Python files

✅ **Do:**
- Use environment variables
- Keep `.env` in `.gitignore`
- Add to `~/.zshrc` for persistence

---

## Next Steps

### Make API Key Permanent (Recommended):
```bash
echo 'export FINNHUB_API_KEY="YOUR_FINNHUB_API_KEY"' >> ~/.zshrc
source ~/.zshrc
```

### Run Hybrid System:
```bash
python run_hybrid_trading.py
```

**You'll now see:**
- Earnings data in news scoring
- "Consistent beaters" identified
- Earnings-driven opportunities highlighted
- Enhanced catalyst detection

---

## Summary

✅ **API Key Status:** Active and verified  
✅ **Earnings Features:** Fully operational  
✅ **API Calls:** 1,248 events fetched successfully  
✅ **Integration:** Automatic in hybrid system  
✅ **Security:** Properly protected in .env  

🎯 **Ready to use earnings features in your trading system!**
