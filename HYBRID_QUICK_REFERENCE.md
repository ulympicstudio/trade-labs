# Hybrid Trading System - Quick Reference

## 🎯 Overview

The hybrid system combines the **News Arm** (discovers opportunities with positive catalysts) with the **Quant Arm** (validates with 50+ technical indicators) for high-confidence, multi-factor trading signals.

**Default Configuration:**
- **60% Quant** (technical analysis)
- **40% News** (sentiment + catalysts)
- **1% risk per trade**
- **20% max total portfolio risk**
- **100 position capacity**

---

## 🚀 Quick Start

### Run with defaults:
```bash
python run_hybrid_trading.py
```

### Customize in code:
```python
from ib_insync import IB
from run_hybrid_trading import HybridTradingSystem

ib = IB()
ib.connect("127.0.0.1", 7497, clientId=1)

hybrid = HybridTradingSystem(
    ib_connection=ib,
    quant_weight=0.60,
    news_weight=0.40,
    total_capital=100000
)

positions = hybrid.run_full_scan(
    min_news_score=60.0,
    min_quant_score=55.0,
    min_unified_score=65.0,
    min_confidence=60.0,
    news_days_back=3,
    max_positions=50
)
```

---

## ⚙️ Parameter Tuning Guide

### Strategy Profiles

#### **Conservative (Focus on Technicals)**
```python
positions = hybrid.run_full_scan(
    min_news_score=50.0,      # Lower bar for news
    min_quant_score=60.0,     # Higher bar for technicals
    min_unified_score=70.0,   # High combined score
    min_confidence=70.0,      # High confidence only
    news_days_back=7,         # Longer lookback
    max_positions=30          # Fewer positions
)
```
**Use when:** Market is volatile, want only highest-conviction trades

#### **Balanced (Recommended)**
```python
positions = hybrid.run_full_scan(
    min_news_score=60.0,
    min_quant_score=55.0,
    min_unified_score=65.0,
    min_confidence=60.0,
    news_days_back=3,
    max_positions=50
)
```
**Use when:** Normal market conditions, standard risk tolerance

#### **Aggressive (News-Driven)**
```python
# Change weights first
hybrid = HybridTradingSystem(
    quant_weight=0.40,  # Less weight on technicals
    news_weight=0.60    # More weight on news
)

positions = hybrid.run_full_scan(
    min_news_score=65.0,      # Higher bar for news
    min_quant_score=50.0,     # Lower bar for technicals
    min_unified_score=60.0,   # Lower combined threshold
    min_confidence=55.0,      # Accept medium confidence
    news_days_back=2,         # Recent news only
    max_positions=70          # More positions
)
```
**Use when:** Catalyst-driven market, earnings season, strong trends

---

## 📊 Understanding Scores

### News Score (0-100)
- **80-100**: Extremely positive (multiple catalysts, high sentiment)
- **65-80**: Strong positive (good catalyst, positive sentiment)
- **50-65**: Moderate positive (some positive news)
- **35-50**: Neutral (mixed or low volume)
- **0-35**: Negative (bad news, negative sentiment)

**Components:**
- Sentiment (35%): Keyword-based analysis
- Catalyst (30%): Strength of news catalyst
- Buzz (15%): Article volume
- Earnings (20%): Historical beat rate

### Quant Score (0-100)
- **80-100**: Exceptional (strong momentum, low risk, high probability)
- **65-80**: Good (aligned indicators, clear setup)
- **50-65**: Moderate (mixed signals, medium probability)
- **35-50**: Weak (conflicting indicators)
- **0-35**: Poor (unfavorable technicals)

**Components:**
- Momentum (30%): Trend strength, RSI, MACD
- Mean Reversion (25%): Bollinger position, z-score
- Volatility (20%): ATR, historical volatility
- Volume (15%): Volume analysis, accumulation
- Microstructure (10%): Spread, liquidity

### Unified Score (0-100)
**Default:** `(Quant × 0.60) + (News × 0.40)`

- **75-100**: STRONG_BUY (both systems aligned, high confidence)
- **65-75**: BUY (good combined score, one system strong)
- **50-65**: NEUTRAL (mixed signals or medium scores)
- **35-50**: WEAK (conflicting signals or low scores)
- **0-35**: AVOID (both systems negative)

### Confidence (0-100)
Measures agreement between news + quant:
- **80-100**: Very high (both systems agree on signal)
- **60-80**: High (both positive but different strengths)
- **40-60**: Medium (mixed signals)
- **0-40**: Low (conflicting signals)

---

## 🎯 Common Scenarios

### Scenario 1: Not Finding Enough Opportunities
**Problem:** `run_full_scan()` returns 0-5 positions

**Solutions:**
```python
# Option A: Lower thresholds
positions = hybrid.run_full_scan(
    min_news_score=55.0,      # Was 60.0
    min_quant_score=50.0,     # Was 55.0
    min_unified_score=60.0,   # Was 65.0
    min_confidence=55.0       # Was 60.0
)

# Option B: Extend news lookback
positions = hybrid.run_full_scan(
    news_days_back=5          # Was 3
)

# Option C: Increase max positions
positions = hybrid.run_full_scan(
    max_positions=75          # Was 50
)
```

### Scenario 2: Too Many Low-Quality Signals
**Problem:** Getting 40+ positions but many seem weak

**Solutions:**
```python
# Raise quality thresholds
positions = hybrid.run_full_scan(
    min_news_score=65.0,      # Was 60.0
    min_quant_score=60.0,     # Was 55.0
    min_unified_score=70.0,   # Was 65.0
    min_confidence=65.0       # Was 60.0
)
```

### Scenario 3: Want More News-Driven Plays
**Problem:** Need more focus on catalysts (earnings, upgrades)

**Solutions:**
```python
# Increase news weight
hybrid = HybridTradingSystem(
    quant_weight=0.50,   # Was 0.60
    news_weight=0.50     # Was 0.40
)

# Prioritize news quality
positions = hybrid.run_full_scan(
    min_news_score=65.0,      # Higher news bar
    min_quant_score=50.0,     # Lower quant bar
    news_days_back=2          # Recent catalysts only
)
```

### Scenario 4: Want More Technical Confirmation
**Problem:** News is noisy, want stronger technical setups

**Solutions:**
```python
# Increase quant weight
hybrid = HybridTradingSystem(
    quant_weight=0.70,   # Was 0.60
    news_weight=0.30     # Was 0.40
)

# Prioritize quant quality
positions = hybrid.run_full_scan(
    min_news_score=55.0,      # Lower news bar
    min_quant_score=60.0,     # Higher quant bar
    news_days_back=5          # Longer lookback (more stable)
)
```

### Scenario 5: Earnings Season (High Volatility)
**Problem:** Want to capture earnings plays with technical confirmation

**Solutions:**
```python
# Earnings-focused configuration
positions = hybrid.run_full_scan(
    min_news_score=65.0,      # Good catalyst required
    min_quant_score=55.0,     # Some technical support
    min_unified_score=65.0,   # Balanced threshold
    news_days_back=7,         # Capture earnings announcements
    max_positions=40          # Moderate position count
)

# Can also use earnings-specific scorer:
from src.data.news_scorer import NewsScorer
scorer = NewsScorer(earnings_api_key="YOUR_KEY")
earnings_winners = scorer.get_earnings_winners(
    days_ahead=14,
    min_beat_rate=70.0
)
```

---

## 📈 Reading the Output

### Phase 1: News Discovery
```
PHASE 1: NEWS DISCOVERY
Found 3 trending stocks:
  1. VST: 3 articles
  2. AVGO: 2 articles
  3. TRI: 2 articles
```
**Interpretation:** System found stocks with multiple recent articles

### Phase 2: Quant Validation
```
PHASE 2: QUANTITATIVE VALIDATION
Retrieved data for 3 symbols
✅ 2 symbols passed quant validation
```
**Interpretation:** 2/3 candidates have acceptable technical setups

### Phase 3: Unified Scoring
```
Rank  Symbol  Total   Quant   News    Signal      Conf
1     AVGO    76.3    82.1    66.5    STRONG_BUY  89
2     VST     71.2    68.4    75.8    BUY         81
```
**Interpretation:** 
- AVGO: Strong technicals (82.1), good news (66.5), very high confidence (89%)
- VST: Good technicals (68.4), strong news (75.8), high confidence (81%)

### Phase 4: Portfolio Allocation
```
PORTFOLIO ALLOCATION SUMMARY
Total Capital: $100,000
Capital Allocated: $48,500 (48.5%)
Total Risk: $4,250 (4.25%)
Approved Positions: 18
```
**Interpretation:** Portfolio manager approved 18 positions using ~50% capital with 4.25% total risk

---

## 🔧 Advanced Configuration

### Custom Quant/News Weights

#### Day Trading Style (Technical Focus)
```python
hybrid = HybridTradingSystem(
    quant_weight=0.80,  # 80% technical
    news_weight=0.20    # 20% news (exclude noise)
)
```

#### Swing Trading Style (Balanced)
```python
hybrid = HybridTradingSystem(
    quant_weight=0.60,  # 60% technical (default)
    news_weight=0.40    # 40% news
)
```

#### Catalyst Trading Style (News Focus)
```python
hybrid = HybridTradingSystem(
    quant_weight=0.40,  # 40% technical
    news_weight=0.60    # 60% news (chase catalysts)
)
```

### Portfolio Risk Configuration

#### Conservative Risk
```python
from src.quant.portfolio_risk_manager import PortfolioRiskManager

hybrid.portfolio_manager = PortfolioRiskManager(
    total_capital=100000,
    max_positions=30,           # Fewer positions
    max_risk_per_trade_pct=0.5, # 0.5% risk per trade
    max_total_risk_pct=10.0     # 10% max total risk
)
```

#### Moderate Risk (Default)
```python
hybrid.portfolio_manager = PortfolioRiskManager(
    total_capital=100000,
    max_positions=50,
    max_risk_per_trade_pct=1.0,
    max_total_risk_pct=20.0
)
```

#### Aggressive Risk
```python
hybrid.portfolio_manager = PortfolioRiskManager(
    total_capital=100000,
    max_positions=100,          # Maximum positions
    max_risk_per_trade_pct=1.5, # 1.5% risk per trade
    max_total_risk_pct=30.0     # 30% max total risk
)
```

---

## 🎓 Best Practices

### 1. Start Conservative
Run first scan with high thresholds, review output, then adjust.

### 2. Match Market Conditions
- **Trending market**: Higher quant weight (0.70), lower thresholds
- **Choppy market**: Higher confidence requirement (70%), fewer positions
- **Earnings season**: Balanced weights, news_days_back=7

### 3. Review Top Opportunity Breakdown
Always check the detailed breakdown printed for the #1 opportunity. Verify:
- Both signals agree (quant + news)
- Confidence is high (>70%)
- Risk:Reward is acceptable (>2.0)

### 4. Paper Trade First
Run system in paper trading mode for 1-2 weeks before going live.

### 5. Monitor Performance
Track which parameter sets work best for your style:
```python
# Save results
with open('scan_results.json', 'w') as f:
    json.dump({
        'timestamp': datetime.now().isoformat(),
        'parameters': {
            'min_news_score': 60.0,
            'min_quant_score': 55.0,
            # ... etc
        },
        'positions': approved_positions
    }, f)
```

---

## 🚨 Troubleshooting

### "No news-driven candidates found"
- Lower `min_news_score` to 55.0 or 50.0
- Increase `news_days_back` to 5 or 7
- Market may be quiet (normal on weekends/holidays)

### "No candidates passed quant validation"
- Lower `min_quant_score` to 50.0 or 45.0
- News candidates may have poor technicals (expected)
- Check IB connection and data availability

### "No positions approved by portfolio manager"
- Lower `min_unified_score` to 60.0 or 55.0
- Lower `min_confidence` to 55.0 or 50.0
- Increase `max_positions` to 75 or 100
- Check portfolio manager risk limits

### "IB connection refused"
- Ensure TWS or IB Gateway is running
- Check port (7497 for TWS, 4001 for Gateway)
- Verify API is enabled in TWS settings

---

## 📞 Support

For issues or questions:
1. Check logs in console output
2. Review parameter settings above
3. Test with `test_news_integration.py` (news-only)
4. Test with `test_quant_system.py` (quant-only)
5. Verify IB connection with simple script

---

## 🎯 Summary

**Default Hybrid Configuration:**
```python
# Balanced approach for swing trading
hybrid = HybridTradingSystem(
    quant_weight=0.60,
    news_weight=0.40,
    total_capital=100000
)

positions = hybrid.run_full_scan(
    min_news_score=60.0,
    min_quant_score=55.0,
    min_unified_score=65.0,
    min_confidence=60.0,
    news_days_back=3,
    max_positions=50
)
```

**Run:** `python run_hybrid_trading.py`

**Expected Results:**
- 3-8 news-driven candidates discovered
- 2-5 candidates pass quant validation
- 10-30 positions approved by portfolio manager
- Total time: 60-90 seconds
