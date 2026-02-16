# Backtesting System - Complete Documentation

## 🎯 Overview

The backtesting engine tests the Trade Labs hybrid trading system on historical data to validate performance before risking real capital.

**What it does:**
- Simulates trades on past market data
- Tracks entry/exit signals
- Manages position sizing and risk
- Calculates comprehensive performance metrics
- Generates equity curves and trade logs

---

## 📊 How It Works

### 1. **Historical Data Fetching**
- Connects to Interactive Brokers
- Downloads historical price data (OHLCV)
- Caches data locally (avoid re-fetching)
- Validates data quality

### 2. **Day-by-Day Simulation**
- Scans for opportunities (configurable frequency)
- Enters trades based on scoring system
- Monitors open positions
- Executes stops, targets, and time exits
- Tracks equity day-by-day

### 3. **Performance Analysis**
- Win/loss statistics
- Risk-adjusted returns (Sharpe, Sortino)
- Maximum drawdown
- Profit factor
- Trade-by-trade breakdown

---

## 🚀 How to Run a Backtest

### Quick Test (30 days, 5 symbols)
```bash
cd /Users/umronalkotob/trade-labs
python test_backtest.py
```
**Time:** ~2-3 minutes  
**Purpose:** Verify the system works

---

### Full Backtest (6 months, 50 symbols)
```bash
cd /Users/umronalkotob/trade-labs
python run_backtest.py
```
**Time:** ~15-30 minutes  
**Purpose:** Comprehensive performance validation

---

## ⚙️ Configuration

### Adjusting Backtest Parameters

Open `run_backtest.py` and modify:

```python
# Date range
end_date = datetime.now()
start_date = end_date - timedelta(days=180)  # 6 months

# Capital
initial_capital = 100000.0

# Risk parameters
max_positions = 30              # Max concurrent positions
max_holding_days = 20           # Max days to hold
scan_frequency_days = 3         # Days between scans

# Scoring thresholds
min_unified_score = 65.0        # Minimum score to enter
min_confidence = 60.0           # Minimum confidence
```

---

### Conservative Settings (Fewer Trades)
```python
max_positions = 20
max_holding_days = 10
scan_frequency_days = 5
min_unified_score = 70.0
min_confidence = 70.0
```

### Aggressive Settings (More Trades)
```python
max_positions = 50
max_holding_days = 30
scan_frequency_days = 1
min_unified_score = 60.0
min_confidence = 55.0
```

---

## 📈 Understanding the Results

### Performance Metrics

**Total Return**
- Total profit/loss in dollars and percentage
- Shows overall system profitability

**Max Drawdown**
- Largest peak-to-trough decline
- Shows worst-case risk
- Lower is better

**Sharpe Ratio**
- Risk-adjusted returns
- > 1.0 is good, > 2.0 is excellent
- Measures return per unit of risk

**Sortino Ratio**
- Like Sharpe but only considers downside risk
- > 1.0 is good, > 3.0 is excellent

**Win Rate**
- Percentage of profitable trades
- 50-60% is typical for swing trading

**Profit Factor**
- Gross profit ÷ Gross loss
- > 1.5 is good, > 2.0 is excellent

**Expectancy**
- Average profit per trade
- Must be positive for profitability

---

### Sample Output

```
BACKTEST RESULTS
================================================================================

Period: 2025-08-14 to 2026-02-14 (184 days)

📊 PERFORMANCE:
  Total Return:    $12,450.00 (+12.45%)
  Max Drawdown:    $3,200.00 (3.20%)
  Sharpe Ratio:    1.85
  Sortino Ratio:   2.63

📈 TRADES:
  Total Trades:    65
  Winners:         38 (58.5%)
  Losers:          27 (41.5%)

💰 WIN/LOSS:
  Average Win:     $850.00
  Average Loss:    $425.00
  Largest Win:     $2,340.00
  Largest Loss:    $980.00
  Profit Factor:   1.92
  Expectancy:      $191.54

⏱️  HOLDING PERIOD:
  Average:         8.3 days
  Maximum:         20 days
```

---

## 📁 Output Files

After running a backtest, you'll get:

### 1. **Trade Log CSV**
`backtest_trades_YYYYMMDD_HHMMSS.csv`

Contains every trade:
- Symbol
- Entry/exit dates
- Entry/exit prices
- Shares
- P&L
- Exit reason (stop, target, time)
- Days held

### 2. **Equity Curve CSV**
`backtest_equity_YYYYMMDD_HHMMSS.csv`

Day-by-day equity:
- Date
- Total equity
- Use for charting performance

---

## 🎓 Interpreting Results

### Good Results (System is Working)
✅ Positive total return  
✅ Win rate 50-65%  
✅ Profit factor > 1.5  
✅ Sharpe ratio > 1.0  
✅ Max drawdown < 15%  
✅ Positive expectancy

### Warning Signs (Adjust Parameters)
⚠️ Negative total return  
⚠️ Win rate < 45%  
⚠️ Profit factor < 1.2  
⚠️ Sharpe ratio < 0.5  
⚠️ Max drawdown > 25%  
⚠️ Negative expectancy

---

## 🔧 Optimization Process

### Step 1: Baseline Test
Run with default settings:
```bash
python run_backtest.py
```

### Step 2: Analyze Results
Look at:
- Win rate (should be 50-60%)
- Profit factor (should be > 1.5)
- Max drawdown (should be < 15%)

### Step 3: Adjust Parameters

**If win rate is low (<45%):**
- Increase `min_unified_score` (more selective)
- Increase `min_confidence`
- Decrease `max_positions` (focus on best)

**If drawdown is high (>20%):**
- Decrease `max_positions` (less exposure)
- Decrease `max_holding_days` (cut losers faster)
- Tighten risk per trade (modify code: reduce from 1% to 0.5%)

**If trades are too few:**
- Decrease `min_unified_score`
- Decrease `min_confidence`
- Increase `scan_frequency_days`

### Step 4: Re-test
Run backtest again with new parameters and compare results.

### Step 5: Validate
Test multiple time periods:
- Last 3 months
- Last 6 months
- Last 12 months

If system is profitable across all periods, it's robust.

---

## 💡 Tips for Better Backtesting

### 1. **Use Realistic Data**
- Backtest on liquid stocks (high volume)
- Avoid penny stocks (unrealistic fills)
- Include transaction costs (modify code to add commission)

### 2. **Test Multiple Periods**
- Bull markets (2023-2024)
- Bear markets (2022)
- Sideways markets (2015-2016)

### 3. **Watch for Overfitting**
- Don't optimize too much
- If results are "too good" (90% win rate), it's likely overfit
- Target realistic metrics (55-65% win rate)

### 4. **Consider Market Regime**
```python
# Add regime filter in run_backtest.py
# Example: Only trade when SPY is above 200 SMA
```

### 5. **Validate with Walk-Forward**
- Backtest on Period A (training)
- Test on Period B (validation)
- Compare results

---

## 🚨 Limitations

### What Backtest Includes
✅ Historical prices  
✅ Technical indicators  
✅ Position sizing  
✅ Stop loss / profit targets  
✅ Risk management

### What Backtest Doesn't Include (Simplified)
⚠️ Real-time news (uses historical prices only)  
⚠️ Slippage (assumes fills at exact price)  
⚠️ Commission (not included by default)  
⚠️ Liquidity constraints  
⚠️ Market impact

**Why:** Full news integration would require historical news data (expensive). The backtest focuses on technical validation.

**Solution:** Use results as a baseline. Real trading will have:
- ~0.5-1% lower returns (slippage/commission)
- Slightly different fills
- But overall patterns should hold

---

## 📊 Advanced: Customizing the Backtest

### Add Commission
Edit `src/backtest/backtest_engine.py`:

```python
# In _close_trade method, add:
commission = shares * 0.005  # $0.005 per share
trade.pnl -= commission
```

### Change Position Sizing
Edit `src/backtest/backtest_engine.py`:

```python
# In _scan_and_enter_trades, change:
risk_amount = self.current_capital * 0.005  # 0.5% risk instead of 1%
```

### Add Custom Filters
Edit `run_backtest.py`:

```python
# Example: Only trade high-volume stocks
universe = ['AAPL', 'MSFT', 'GOOGL', ...] # Add more liquid stocks
```

---

## 📞 Quick Reference

### Run Quick Test
```bash
python test_backtest.py
```
**Time:** 2-3 minutes  
**Use:** Verify system works

### Run Full Backtest
```bash
python run_backtest.py
```
**Time:** 15-30 minutes  
**Use:** Comprehensive testing

### Check Results
```bash
# Trade log
open backtest_trades_*.csv

# Equity curve
open backtest_equity_*.csv
```

### Adjust Settings
```bash
# Edit backtest parameters
open -a TextEdit run_backtest.py
```

---

## 🎯 Next Steps After Backtesting

1. **Review Results**
   - Check if metrics meet your goals
   - Identify strengths/weaknesses

2. **Optimize Parameters**
   - Adjust scoring thresholds
   - Test different holding periods
   - Re-run and compare

3. **Paper Trade**
   - Run hybrid system in real-time
   - Track results without real money
   - Compare to backtest

4. **Live Trading**
   - Start with small capital
   - Monitor performance
   - Scale up if consistent

---

## 🔍 Troubleshooting

### "No historical data fetched"
**Problem:** IB connection or data access issue  
**Solution:**
1. Check IB TWS is running
2. Verify API is enabled
3. Check market data subscriptions
4. Try fewer symbols first

### "Backtest too slow"
**Problem:** Fetching data for many symbols  
**Solution:**
1. Reduce universe size (start with 10-20 symbols)
2. Shorten date range (3 months instead of 6)
3. Use cached data (run twice - second is faster)

### "No trades executed"
**Problem:** Thresholds too strict  
**Solution:**
1. Lower `min_unified_score` to 60
2. Lower `min_confidence` to 55
3. Increase `scan_frequency_days` to 1

### "Results are unrealistic"
**Problem:** Overfitting or data issues  
**Solution:**
1. Test on different time periods
2. Add commission/slippage
3. Increase `min_unified_score` (be more selective)

---

## 📚 Additional Resources

- **Backtest Engine Code:** `src/backtest/backtest_engine.py`
- **Historical Data Manager:** `src/backtest/historical_data.py`
- **Test Script:** `test_backtest.py`
- **Full Backtest:** `run_backtest.py`

---

**Remember:** Backtesting shows what *would have* happened. Past performance doesn't guarantee future results. Use it to validate logic, not predict future returns.

