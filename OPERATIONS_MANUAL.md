# Trade Labs - Operations Manual for Ulympic
**Simple Instructions - No Programming Knowledge Required**

---

## 🎯 What Trade Labs Does

Trade Labs scans the market and finds the best trading opportunities by:
1. **Finding trending stocks** with positive news (earnings beats, upgrades, product launches)
2. **Checking technicals** with 50+ indicators (RSI, MACD, moving averages, etc.)
3. **Combining both** to give you a ranked list of the best trades
4. **Managing risk** so you don't risk more than 1% per trade

**Think of it as your AI trading assistant that does all the analysis and gives you a shopping list of trades.**

---

## ⚙️ SETUP (One-Time Only)

### Before First Use:

1. **Open Terminal**
   - Press `Command + Space`
   - Type "Terminal"
   - Press Enter

2. **Go to Trade Labs Folder**
   ```bash
   cd /Users/umronalkotob/trade-labs
   ```

3. **Set Your API Key (Permanent)**
   ```bash
  echo 'export FINNHUB_API_KEY="YOUR_FINNHUB_API_KEY"' >> ~/.zshrc
   source ~/.zshrc
   ```
   *(Only need to do this once - it's saved forever)*

4. **Verify Everything Works**
   ```bash
   python preflight_check.py
   ```
   
   ✅ Should say: "ALL SYSTEMS GO"

---

## 🚀 HOW TO TURN ON TRADE LABS

### Step 1: Start IB TWS (Interactive Brokers)
1. Open TWS or IB Gateway application
2. Log in with your credentials
3. Wait for it to fully load (shows green "Connected" status)
4. **Important:** Go to TWS → Configure → API → Settings
   - Check "Enable ActiveX and Socket Clients"
   - Make sure Socket port is 7497
   - Click OK

### Step 2: Open Terminal
1. Press `Command + Space`
2. Type "Terminal"
3. Press Enter

### Step 3: Navigate to Trade Labs
```bash
cd /Users/umronalkotob/trade-labs
```

### Step 4: Run Trade Labs
```bash
/opt/miniconda3/bin/conda run -p /opt/miniconda3/envs/trade-labs --no-capture-output python run_hybrid_trading.py
```

**If you are not using conda:**
```bash
python run_hybrid_trading.py
```

**What Happens Next:**
- System connects to IB
- Scans Google News for trending stocks (~10 seconds)
- Analyzes technicals for each candidate (~30-60 seconds)
- Combines scores and ranks opportunities
- Shows you approved positions
- Saves results to a file

**Total Time:** About 60-90 seconds

---

## 📊 READING THE OUTPUT

### Phase 1: News Discovery
```
PHASE 1: NEWS DISCOVERY
Found 8 trending stocks: VST, AVGO, TRI, CROX, PWR, EOSE, LYEL, RDCM
✅ Discovered 8 news-driven candidates
```
**What this means:** Found 8 stocks with positive news in the last 3 days

---

### Phase 2: Quant Validation
```
PHASE 2: QUANTITATIVE VALIDATION
Retrieved data for 8 symbols
✅ 5 symbols passed quant validation
```
**What this means:** 5 out of 8 have good technical setups

---

### Phase 3: Unified Scoring
```
Rank  Symbol  Total   Quant   News    Signal      Conf  Entry     Stop      Target    R:R
1     AVGO    78.5    85.2    67.8    STRONG_BUY  91    $175.20   $171.85   $183.60   2.5
2     VST     74.3    70.1    81.2    BUY         86    $142.50   $139.10   $151.00   2.5
```

**How to Read This:**
- **Total**: Combined score (higher is better, 75+ is excellent)
- **Quant**: Technical score (60+ is good)
- **News**: Sentiment score (60+ is positive)
- **Signal**: STRONG_BUY = best, BUY = good, NEUTRAL = skip
- **Conf**: Confidence (80+ is very confident)
- **Entry**: Price to buy at
- **Stop**: Price to sell if it goes against you (your protection)
- **Target**: Price goal (where you take profit)
- **R:R**: Risk:Reward ratio (2.5 means you risk $1 to make $2.50)

---

### Phase 4: Portfolio Allocation
```
PORTFOLIO ALLOCATION SUMMARY
Total Capital: $100,000
Capital Allocated: $48,500 (48.5%)
Total Risk: $4,250 (4.25%)
Approved Positions: 18
```

**What this means:**
- You have $100,000 to trade
- System wants to use $48,500 across 18 trades
- If ALL 18 trades hit their stops, you'd lose $4,250 (4.25%)
- Each individual trade risks about 1% ($1,000)

**The list below shows each trade with:**
- How many shares to buy
- How much money to invest
- How much you're risking

---

## 🎮 AVAILABLE COMMANDS

### 1. STANDARD SCAN (Default Settings)
```bash
cd /Users/umronalkotob/trade-labs
python run_hybrid_trading.py
```
**What it does:** Finds opportunities using balanced settings (60% technicals, 40% news)

---

### 2. CONSERVATIVE SCAN (High Quality Only)
Open `run_hybrid_trading.py` in any text editor and change bottom section to:
```python
# At the bottom of the file, change these numbers:
MIN_NEWS_SCORE = 65.0      # Was 60.0
MIN_QUANT_SCORE = 60.0     # Was 55.0
MIN_UNIFIED_SCORE = 70.0   # Was 65.0
MIN_CONFIDENCE = 70.0      # Was 60.0
MAX_POSITIONS = 30         # Was 50
```

**What it does:** Only shows the absolute best trades, fewer positions

---

### 3. AGGRESSIVE SCAN (More Opportunities)
Change these numbers:
```python
MIN_NEWS_SCORE = 55.0      # Lower bar
MIN_QUANT_SCORE = 50.0     # Lower bar
MIN_UNIFIED_SCORE = 60.0   # Lower bar
MIN_CONFIDENCE = 55.0      # Lower bar
MAX_POSITIONS = 75         # More trades
```

**What it does:** Shows more opportunities, slightly lower quality

---

### 4. NEWS-ONLY SCAN (Find What's Trending)
```bash
cd /Users/umronalkotob/trade-labs
python test_news_integration.py
```

**What it does:** 
- Shows trending stocks with positive news
- No technical analysis
- Good for research/ideas
- Faster (10 seconds)

---

### 5. GET EARNINGS WINNERS
Create a file called `find_earnings.py`:
```python
import os
from src.data.news_scorer import NewsScorer

scorer = NewsScorer(earnings_api_key=os.environ.get('FINNHUB_API_KEY'))

# Find stocks with earnings in next 2 weeks + strong history
winners = scorer.get_earnings_winners(
    days_ahead=14,
    min_beat_rate=70.0
)

print(f"\nFound {len(winners)} earnings winners:\n")
for i, winner in enumerate(winners[:20], 1):
    print(f"{i}. {winner['symbol']}: "
          f"{winner['beat_rate']:.0f}% beat rate, "
          f"{winner['days_until']} days until earnings")
```

Run it:
```bash
python find_earnings.py
```

**What it does:** Shows stocks that consistently beat earnings estimates

---

### 6. CHECK SYSTEM STATUS
```bash
cd /Users/umronalkotob/trade-labs
python preflight_check.py
```

**What it does:** 
- Checks if everything is working
- Verifies IB connection
- Shows if API key is active
- Takes 5 seconds

---

### 7. TEST NEWS SYSTEM (No IB Required)
```bash
cd /Users/umronalkotob/trade-labs
python test_news_integration.py
```

**What it does:**
- Tests news fetching
- Shows sentiment analysis
- Doesn't need IB connection
- Good for testing when market is closed

---

### 8. RUN BACKTEST (Test on Historical Data)
```bash
cd /Users/umronalkotob/trade-labs
python run_backtest.py
```

**What it does:**
- Tests system on past 6 months of data
- Shows what would have happened
- Validates strategy performance
- Takes 15-30 minutes
- Generates performance report + trade log

**Quick test version:**
```bash
python test_backtest.py
```
Takes 2-3 minutes, tests last 30 days only

---

## ⏸️ HOW TO PAUSE / STOP TRADE LABS

### To Stop a Running Scan:
- Press `Control + C` in the Terminal window
- System stops immediately
- No harm done

### To Pause Between Scans:
Trade Labs doesn't run continuously - it's a "scan and stop" system.
- Run it when you want to scan
- Review the results
- Run it again later if you want updated results

**There's nothing to pause because it stops automatically after each scan.**

---

## 🔄 HOW OFTEN TO RUN IT

### Recommended Schedule:

**Pre-Market (8:00 AM ET):**
```bash
python run_hybrid_trading.py
```
- Review opportunities before market opens
- Place orders during pre-market or at open

**Mid-Morning (10:30 AM ET):**
```bash
python run_hybrid_trading.py
```
- Re-scan after initial volatility settles
- Look for new setups

**Mid-Day (1:00 PM ET) - Optional:**
```bash
python run_hybrid_trading.py
```
- Check for afternoon opportunities

**End of Day (3:00 PM ET) - Optional:**
```bash
python run_hybrid_trading.py
```
- Look for swing trades to hold overnight

---

## � HOW TO ACTUALLY BUY AND SELL STOCKS

**IMPORTANT:** Trade Labs is a scanning tool, not an autopilot.

**The Process:**
1. **Run scan** → System finds opportunities
2. **Review results** → You decide which to take
3. **Execute in TWS** → You manually place orders
4. **Manage exits** → Monitor stops and targets

**Complete Instructions:**
👉 **See [EXECUTION_GUIDE.md](EXECUTION_GUIDE.md)** for step-by-step execution instructions

**Quick Summary:**

### Step 1: Run Scan
```bash
python run_hybrid_trading.py
```

### Step 2: Review Results
Look at the approved positions table. Pick 5-10 best trades.

### Step 3: Place Orders in TWS

**For each trade:**
1. Open TWS order entry
2. Symbol: [From scan]
3. Action: BUY
4. Quantity: [From scan - "Shares" column]
5. Order Type: LIMIT
6. Limit Price: [From scan - "Entry" column]
7. Click "Transmit"

### Step 4: Set Stops (After Order Fills)

**For each filled order:**
1. Right-click position in TWS
2. Select "Attach Order" → "Stop"
3. Action: SELL
4. Stop Price: [From scan - "Stop" column]
5. Time in Force: GTC
6. Click "Transmit"

### Step 5: Set Targets (Optional)

1. Right-click position
2. Select "Attach Order" → "Limit"
3. Action: SELL
4. Limit Price: [From scan - "Target" column]
5. Time in Force: GTC
6. Click "Transmit"

**Now your position is protected with a stop loss and has a profit target.**

---

## �📝 HOW TO GET MANUAL REPORTS

### Get Full Report (All Phases):
```bash
python run_hybrid_trading.py
```
This generates the complete analysis and saves it automatically.

**Output File Location:**
```
/Users/umronalkotob/trade-labs/hybrid_scan_YYYYMMDD_HHMMSS.json
```

Example: `hybrid_scan_20260214_093045.json`

### View Saved Reports:
1. Open Finder
2. Navigate to `/Users/umronalkotob/trade-labs/`
3. Look for files starting with `hybrid_scan_`
4. Open with TextEdit or any text editor

**The JSON file contains:**
- All approved positions
- Entry/stop/target prices
- Position sizes
- Risk amounts
- Scores and signals

---

### Get News-Only Report:
```bash
python test_news_integration.py
```

Shows:
- Trending stocks
- Sentiment analysis
- News catalysts
- No positions (just information)

---

### Get Specific Symbol Analysis:

Create a file called `check_symbol.py`:
```python
from src.data.news_scorer import NewsScorer
import os

scorer = NewsScorer(earnings_api_key=os.environ.get('FINNHUB_API_KEY'))

# Change these symbols to whatever you want to check
symbols = ['AAPL', 'MSFT', 'NVDA', 'TSLA', 'GOOGL']

print("\n" + "="*80)
print("SYMBOL ANALYSIS REPORT")
print("="*80 + "\n")

for symbol in symbols:
    score = scorer.score_symbol(symbol, days_back=7)
    if score:
        print(f"{symbol}:")
        print(f"  News Score: {score.total_news_score:.1f}/100")
        print(f"  Sentiment: {score.avg_sentiment:+.3f} ({score.news_signal})")
        print(f"  Articles: {score.article_count} in last 7 days")
        if score.strongest_catalyst:
            print(f"  Catalyst: {score.strongest_catalyst}")
        if score.has_upcoming_earnings:
            print(f"  Earnings in {score.days_until_earnings} days (beat rate: {score.historical_beat_rate:.0f}%)")
        print()
    else:
        print(f"{symbol}: No news data\n")
```

Run it:
```bash
python check_symbol.py
```

---

## 🛠️ ADJUSTING SETTINGS

All settings are at the bottom of `run_hybrid_trading.py`:

### Open the file:
```bash
open -a TextEdit run_hybrid_trading.py
```

### Scroll to the bottom and find:
```python
# Configuration
TOTAL_CAPITAL = 100000.0

# Hybrid system parameters
QUANT_WEIGHT = 0.60  # 60% technical analysis
NEWS_WEIGHT = 0.40   # 40% news sentiment

# Filtering parameters
MIN_NEWS_SCORE = 60.0      # News discovery threshold
MIN_QUANT_SCORE = 55.0     # Quant validation threshold
MIN_UNIFIED_SCORE = 65.0   # Portfolio allocation threshold
MIN_CONFIDENCE = 60.0      # Minimum confidence level
NEWS_DAYS_BACK = 3         # Look back 3 days for news
MAX_POSITIONS = 50         # Maximum concurrent positions
```

### What Each Setting Does:

**TOTAL_CAPITAL**: Your trading account size
- Example: 100000.0 = $100,000

**QUANT_WEIGHT / NEWS_WEIGHT**: How much to trust each system
- 0.60 / 0.40 = Balanced (60% technicals, 40% news)
- 0.70 / 0.30 = Trust technicals more
- 0.40 / 0.60 = Trust news more

**MIN_NEWS_SCORE**: How good news has to be (0-100)
- 60 = Good balance
- 65 = Only strong positive news
- 55 = More opportunities

**MIN_QUANT_SCORE**: How good technicals have to be (0-100)
- 55 = Good balance
- 60 = Only strong technical setups
- 50 = More opportunities

**MIN_UNIFIED_SCORE**: Final bar for approval (0-100)
- 65 = Balanced
- 70 = High quality only
- 60 = More trades

**MIN_CONFIDENCE**: How sure system has to be (0-100)
- 60 = Balanced
- 70 = Very confident only
- 55 = Accept medium confidence

**NEWS_DAYS_BACK**: How far back to look for news
- 3 = Recent news only (fast moving)
- 5 = Medium lookback
- 7 = Longer history (more stable)

**MAX_POSITIONS**: Maximum number of trades
- 50 = Balanced
- 30 = Conservative (fewer trades)
- 75 = Aggressive (more trades)

### After Changing Settings:
1. Save the file
2. Run the scan again: `python run_hybrid_trading.py`

---

## 🎯 COMMON USE CASES

### "I Want to See What's Hot Today"
```bash
python test_news_integration.py
```
Takes 30 seconds, shows trending stocks.

---

### "I Want High-Quality Trades Only"
Change settings to:
```python
MIN_UNIFIED_SCORE = 70.0
MIN_CONFIDENCE = 70.0
MAX_POSITIONS = 30
```

---

### "I Want More Opportunities"
Change settings to:
```python
MIN_NEWS_SCORE = 55.0
MIN_QUANT_SCORE = 50.0
MIN_UNIFIED_SCORE = 60.0
MAX_POSITIONS = 75
```

---

### "I Want to Focus on Earnings"
```bash
python find_earnings.py
```

---

### "I Want to Check If System Is Working"
```bash
python preflight_check.py
```

---

### "I Want to Research a Specific Stock"
Edit `check_symbol.py` and add your symbols, then:
```bash
python check_symbol.py
```

---

## 🔧 TROUBLESHOOTING

### Problem: "Cannot connect to IB"
**Solution:**
1. Make sure TWS or IB Gateway is running
2. Check you're logged in
3. Verify API is enabled (TWS → Configure → API → Settings)
4. Make sure port is 7497

---

### Problem: "No news-driven candidates found"
**Solution:**
1. Normal on weekends or holidays
2. Try increasing `NEWS_DAYS_BACK` to 5 or 7
3. Try lowering `MIN_NEWS_SCORE` to 55

---

### Problem: "No positions approved"
**Solution:**
Lower the thresholds:
```python
MIN_UNIFIED_SCORE = 60.0
MIN_CONFIDENCE = 55.0
```

---

### Problem: "Too many low-quality trades"
**Solution:**
Raise the thresholds:
```python
MIN_UNIFIED_SCORE = 70.0
MIN_CONFIDENCE = 70.0
```

---

### Problem: "System is slow"
**Normal:** First run takes 60-90 seconds (fetching data)
**If unusually slow:**
1. Check internet connection
2. Check IB TWS is responsive
3. Try reducing `MAX_POSITIONS` to 30

---

### Problem: "API key not working"
**Solution:**
```bash
export FINNHUB_API_KEY='YOUR_FINNHUB_API_KEY'
python test_finnhub_api.py
```

---

## 📋 DAILY WORKFLOW EXAMPLE

### Morning Routine (15 minutes):

**8:00 AM - Before Market Opens:**
1. Open Terminal
2. `cd /Users/umronalkotob/trade-labs`
3. `python preflight_check.py` (verify system)
4. Start IB TWS
5. `python run_hybrid_trading.py` (get opportunities)
6. Review the list of approved positions
7. Decide which trades to take
8. Place orders in TWS

**10:30 AM - After Opening Volatility:**
1. `python run_hybrid_trading.py` (refresh scan)
2. Look for any new setups
3. Adjust existing positions if needed

**End of Day:**
1. Review open positions
2. Check exit signals
3. Plan for tomorrow

---

## 💡 PRO TIPS

### Tip 1: Save Your Favorite Settings
Make a copy of `run_hybrid_trading.py`:
```bash
cp run_hybrid_trading.py run_hybrid_conservative.py
cp run_hybrid_trading.py run_hybrid_aggressive.py
```

Edit each one with different settings, then run:
```bash
python run_hybrid_conservative.py  # High quality
python run_hybrid_aggressive.py    # More trades
```

---

### Tip 2: Create a Quick Launch Script
Create a file called `morning_scan.sh`:
```bash
#!/bin/bash
cd /Users/umronalkotob/trade-labs
echo "Running system check..."
python preflight_check.py
echo ""
echo "Running hybrid scan..."
python run_hybrid_trading.py
```

Make it executable:
```bash
chmod +x morning_scan.sh
```

Run it:
```bash
./morning_scan.sh
```

---

### Tip 3: Compare Different Time Frames
```bash
# Recent news (fast movers)
NEWS_DAYS_BACK = 2

# Medium term (balanced)
NEWS_DAYS_BACK = 5

# Longer term (more stable)
NEWS_DAYS_BACK = 7
```

---

## 🎓 UNDERSTANDING THE SIGNALS

### STRONG_BUY
- Both news and technicals are excellent
- High confidence (80%+)
- These are your best opportunities
- Consider taking full position size

### BUY
- Either news or technicals is strong
- Good confidence (60-80%)
- Solid opportunities
- Consider normal position size

### NEUTRAL
- Mixed signals or low scores
- Medium confidence
- **SKIP THESE** - not worth the risk

---

## 📊 POSITION SIZING EXPLAINED

System uses **1% risk per trade**:

**Example:**
- Account: $100,000
- Risk per trade: $1,000 (1%)
- Stock price: $100
- Stop price: $95
- Risk per share: $5

**Calculation:**
- Shares = $1,000 ÷ $5 = 200 shares
- Total investment: 200 × $100 = $20,000

**If stop hits:**
- Loss = 200 × $5 = $1,000 (exactly 1% of account)

**If target hits (2.5:1 R:R):**
- Gain = $1,000 × 2.5 = $2,500

---

## 🚦 TRAFFIC LIGHT SYSTEM

Think of scores like traffic lights:

**🟢 GREEN (70-100):** GO - Excellent opportunity
**🟡 YELLOW (60-70):** CAUTION - Good but not great
**🔴 RED (0-60):** STOP - Skip this trade

---

## 📞 QUICK REFERENCE CARD

```
DAILY USE:
  Check system:  python preflight_check.py
  Full scan:     python run_hybrid_trading.py
  News only:     python test_news_integration.py
  
STOP RUNNING:
  Press: Control + C

LOCATION:
  cd /Users/umronalkotob/trade-labs

FILES:
  Settings:    run_hybrid_trading.py (bottom section)
  Results:     hybrid_scan_*.json files
  
HELP:
  Read:        HYBRID_QUICK_REFERENCE.md
  Detailed:    NEWS_SYSTEM_COMPLETE.md
```

---

## 🎯 BOTTOM LINE

**Trade Labs is a scanning tool that:**
1. Runs when you tell it to
2. Takes 60-90 seconds
3. Gives you a ranked list of trades
4. Stops automatically
5. Saves results to a file

**You control:**
- When to run it
- What settings to use
- Which trades to take
- Position sizes (it suggests, you decide)

**Think of it like a calculator:**
- You input: "Find me trades"
- It outputs: "Here are the best ones"
- You review and decide
- You execute in TWS manually

**It's a tool that helps you, not an autopilot.**

---

Need more help? Check:
- **HYBRID_QUICK_REFERENCE.md** - Parameter tuning
- **NEWS_SYSTEM_COMPLETE.md** - News system details
- **READY_TO_RUN.md** - System status
- **FINNHUB_SETUP_COMPLETE.md** - Earnings features

