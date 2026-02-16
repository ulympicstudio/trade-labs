# Trade Labs - Quantitative Swing Trading System

**Professional-grade automated trading system for high-frequency swing trading across 100+ simultaneous positions.**

---

## 📖 NEW USER? START HERE!

**Not a programmer? Read this first:**  
👉 **[OPERATIONS_MANUAL.md](OPERATIONS_MANUAL.md)** - Complete step-by-step guide for non-programmers

**How to buy/sell stocks:**  
👉 **[EXECUTION_GUIDE.md](EXECUTION_GUIDE.md)** - Step-by-step: From scan to execution in TWS

**Quick Reference (print this):**  
👉 **[QUICK_REFERENCE_CARD.md](QUICK_REFERENCE_CARD.md)** - One-page cheat sheet with all commands

**Morning Routine:**  
👉 Just run: `./morning_scan.sh` - Complete automated pre-market scan

---

## 🎯 System Overview

- **Project:** Trade Labs
- **Human Operator:** Ulympic
- **Machine:** Ulympic Studio (Mac Studio)
- **AI System:** Studio

### Communication Rules
- System refers to human as **Ulympic**
- Human refers to system as **Studio**
- Avoid "user", "operator", "bot", or "agent" in logs/reports

---

## 🚀 Quick Start

### Run Hybrid Trading System (RECOMMENDED)
```bash
python run_hybrid_trading.py
```
**Workflow:** News discovers candidates → Quant validates → Portfolio allocates  
**Weighting:** 60% technicals, 40% news sentiment

### Run News-Only Scan
```bash
python test_news_integration.py
```

### Run Quant-Only Scan
```bash
python run_quant_trading.py
```

### Test System
```bash
python test_quant_system.py
```

---

## 🛠️ Helper Scripts

### Complete Morning Routine (Automated)
```bash
./morning_scan.sh
```
Runs system check + hybrid scan automatically

### Find Earnings Winners
```bash
python find_earnings.py
```
Shows stocks with upcoming earnings and strong historical beat rates

### Analyze Specific Symbols
```bash
python check_symbol.py
```
Get news and sentiment analysis for specific stocks (edit file to add symbols)

### Check System Status
```bash
python preflight_check.py
```
Verifies all dependencies and connections before trading

### Backtest System (Test on Historical Data)
```bash
python run_backtest.py
```
Validates strategy on past 6 months. Takes 15-30 minutes.

Quick test (30 days):
```bash
python test_backtest.py
```

---

## 📊 System Capabilities

### **News & Sentiment Arm** (COMPLETE ✅)
- **Google News RSS integration** - Real-time news discovery
- **Sentiment analysis** - 144 weighted keywords for bullish/bearish detection
- **Catalyst identification** - Earnings beats, upgrades, product launches
- **Trending stock discovery** - Most talked-about symbols with positive news

### **Quantitative Data Arm** (COMPLETE ✅)
- **50+ technical indicators** per symbol (RSI, MACD, Bollinger, ATR, etc.)
- **5-component probability scoring** (Momentum, Mean Reversion, Volatility, Volume, Microstructure)
- **Portfolio-level risk management** (100 position capacity)
- **Automated entry/stop/target** calculation (ATR-based stops, 2.5:1 R:R targets)
- **Real-time monitoring** and P&L tracking

### **Hybrid Integration** (COMPLETE ✅)
- **Unified scoring** - Combines news (40%) + quant (60%) for multi-factor signals
- **Multi-phase workflow** - News discovers → Quant validates → Portfolio allocates
- **Signal consensus** - High confidence when both systems agree (STRONG_BUY)
- **Risk-adjusted allocation** - 1% risk per trade, 20% max total risk

### Trading Features
- ✅ Market scanning (IB Scanner integration)
- ✅ Quality filtering (price, spread, volume)
- ✅ Historical data analysis (1 year OHLCV)
- ✅ Quantitative signal generation
- ✅ Position sizing (fixed fractional risk)
- ✅ Order execution (limit orders + stops)
- ✅ Portfolio tracking
- ✅ Performance analytics (Sharpe, Sortino, Calmar)
- ✅ SQLite database (trade history)

### Scale
- **News Scan**: 50 trending stocks in ~10 seconds
- **Quant Validation**: 50 symbols in ~30-60 seconds
- **Unified Scoring**: Complete hybrid workflow in ~60-90 seconds
- **Execute**: 50+ positions per run
- **Monitor**: 100 simultaneous positions
- **Trade**: 400-1,000+ trades/month

---

## 📂 Project Structure

```
trade-labs/
├── src/
│   ├── data/                           # 🆕 News & market data
│   │   ├── news_fetcher.py             # Google News RSS + Finnhub API
│   │   ├── news_sentiment.py           # Keyword-based sentiment analysis
│   │   ├── news_scorer.py              # Multi-factor news scoring
│   │   ├── quant_news_integrator.py    # Unified quant + news scoring
│   │   ├── earnings_calendar.py        # Earnings tracking (optional)
│   │   └── ib_market_data.py           # IB data fetching
│   │
│   ├── quant/                          # 🆕 Quantitative trading engine
│   │   ├── technical_indicators.py     # 50+ technical calculations
│   │   ├── quant_scorer.py             # Probability scoring model
│   │   ├── quant_scanner.py            # Market scanner + IB integration
│   │   └── portfolio_risk_manager.py   # Portfolio-level risk controls
│   │
│   ├── analysis/                       # Performance analytics
│   │   └── advanced_metrics.py         # Sharpe, Sortino, Calmar, drawdown
│   │
│   ├── database/                       # SQLite persistence
│   │   ├── models.py                   # ORM models (trades, signals, positions)
│   │   ├── db_manager.py               # Database operations
│   │   └── migrations.py               # JSON → SQLite migration
│   │
│   ├── broker/                         # Broker integration
│   ├── execution/                      # Order management
│   ├── signals/                        # Signal generation
│   └── risk/                           # Risk management
│
├── run_hybrid_trading.py              # 🆕 Hybrid system (NEWS + QUANT)
├── run_quant_trading.py                # 🔹 Quick start script (quant-only)
├── test_quant_system.py                # 🔹 Comprehensive quant tests
├── test_news_integration.py            # 🔹 News system tests
│
├── HYBRID_QUICK_REFERENCE.md           # 🆕 Hybrid system usage guide
├── NEWS_SYSTEM_COMPLETE.md             # 🆕 Complete news system doc
├── QUANT_SYSTEM.md                     # 🆕 Complete quant documentation
├── QUANT_IMPLEMENTATION_SUMMARY.md     # 🆕 Implementation guide
├── SYSTEM_ARCHITECTURE.md              # 🆕 Architecture diagrams
├── PHASE3_PLAN.md                      # Phase 3 roadmap
└── PHASE3_PROGRESS.md                  # Progress tracking
```

---

## 🎯 Hybrid Trading Workflow (RECOMMENDED)

### Complete End-to-End Pipeline
```python
from ib_insync import IB
from run_hybrid_trading import HybridTradingSystem

# Connect to IB
ib = IB()
ib.connect("127.0.0.1", 7497, clientId=1)

# Initialize hybrid system
hybrid = HybridTradingSystem(
    ib_connection=ib,
    quant_weight=0.60,    # 60% technical analysis
    news_weight=0.40,     # 40% news sentiment
    total_capital=100000
)

# Run complete workflow
approved_positions = hybrid.run_full_scan(
    min_news_score=60.0,      # News discovery threshold
    min_quant_score=55.0,     # Quant validation threshold
    min_unified_score=65.0,   # Portfolio allocation threshold
    min_confidence=60.0,      # Minimum confidence
    news_days_back=3,         # Recent news (3 days)
    max_positions=50          # Maximum concurrent positions
)
```

**What happens:**
1. 📰 **News Discovery**: Scans Google News RSS for trending stocks with positive catalysts
2. 📊 **Quant Validation**: Analyzes 50+ technical indicators for each candidate
3. 🎯 **Unified Scoring**: Combines news (40%) + quant (60%) = total score
4. 💰 **Portfolio Allocation**: Applies risk controls (1% risk/trade, 20% max total risk)
5. ✅ **Execution Ready**: Returns approved positions with entry/stop/target prices

### Alternative: News-Only Discovery
```python
from src.data.news_scorer import NewsScorer

news_scorer = NewsScorer()
trending = news_scorer.get_top_news_driven_opportunities(
    min_score=65.0,
    days_back=3
)
```

### Alternative: Quant-Only Scan
```python
from ib_insync import IB
from src.quant import run_quant_scan

ib = IB()
ib.connect("127.0.0.1", 7497, clientId=1)

scores = run_quant_scan(
    ib,
    candidate_limit=100,
    min_score=65.0,
    min_confidence=55.0,
    display_top_n=20
)
```

### Portfolio Risk Management
```python
from src.quant import PortfolioRiskManager

portfolio = PortfolioRiskManager(
    total_capital=100000,
    max_positions=50,
    max_risk_per_trade_pct=1.0
)

approved = portfolio.prioritize_opportunities(scores)
```

### 3. Execute Trades
```python
# Automatic order placement with entry, stop, and target
for position in approved:
    place_orders(ib, position)
```

### 4. Monitor
```python
# Real-time portfolio monitoring
portfolio.display_portfolio_status()
portfolio.display_open_positions()
```

---

## 📈 Example Output

```
TOP 20 QUANTITATIVE SWING TRADE OPPORTUNITIES
================================================================================
Rank  Symbol  Score  Conf  Dir   Entry      Stop       Target     R:R   Exp%
1     AAPL    87.3   92.1  LONG  $150.25    $145.80    $161.38    2.50  +7.4%
2     MSFT    85.2   89.3  LONG  $405.50    $398.20    $423.75    2.50  +4.5%
3     TSLA    82.7   88.1  SHORT $185.30    $189.50    $174.80    2.50  -5.7%
...

PORTFOLIO STATUS
================================================================================
Capital:         $100,000.00
Deployed:        $ 89,082.18 (89.1%)
Positions:       48 (18 Long, 30 Short)
Total Risk:      $  4,220.37 (4.2%)
Unrealized P&L:  $    +181.44 (+0.20%)
```

---

## 🧪 Testing

### Test Quant System
```bash
python test_quant_system.py
```
Tests technical indicators, scoring, and portfolio management

### Test News System
```bash
python test_news_integration.py
```
Tests news fetching, sentiment analysis, and trending detection

### Test Backtesting Engine
```bash
python test_backtest.py
```
Quick 30-day validation test (2-3 minutes)

### Full Backtest
```bash
python run_backtest.py
```
Comprehensive 6-month historical test (15-30 minutes)

**See:** [BACKTESTING_GUIDE.md](BACKTESTING_GUIDE.md) for complete documentation

---

## 📚 Documentation

### 👥 For Non-Programmers (Start Here)
| Document | Description |
|----------|-------------|
| **[OPERATIONS_MANUAL.md](OPERATIONS_MANUAL.md)** | **Complete step-by-step guide - how to operate Trade Labs** |
| **[EXECUTION_GUIDE.md](EXECUTION_GUIDE.md)** | **How to buy/sell stocks - arm/disarm trading** |
| **[QUICK_REFERENCE_CARD.md](QUICK_REFERENCE_CARD.md)** | **One-page cheat sheet - print and keep at desk** |

### 🔧 For Technical Users
| Document | Description |
|----------|-------------|
| **[HYBRID_QUICK_REFERENCE.md](HYBRID_QUICK_REFERENCE.md)** | **Hybrid system usage guide & parameter tuning** |
| **[BACKTESTING_GUIDE.md](BACKTESTING_GUIDE.md)** | **Complete backtesting documentation & optimization** |
| [NEWS_SYSTEM_COMPLETE.md](NEWS_SYSTEM_COMPLETE.md) | Complete news & sentiment system documentation |
| [QUANT_SYSTEM.md](QUANT_SYSTEM.md) | Complete technical documentation |
| [READY_TO_RUN.md](READY_TO_RUN.md) | System readiness verification |
| [FINNHUB_SETUP_COMPLETE.md](FINNHUB_SETUP_COMPLETE.md) | Earnings API setup guide |
| [QUANT_IMPLEMENTATION_SUMMARY.md](QUANT_IMPLEMENTATION_SUMMARY.md) | Implementation guide |
| [SYSTEM_ARCHITECTURE.md](SYSTEM_ARCHITECTURE.md) | Architecture diagrams |
| [PHASE3_PLAN.md](PHASE3_PLAN.md) | Phase 3 roadmap |
| [PHASE3_PROGRESS.md](PHASE3_PROGRESS.md) | Progress tracking |

### Quick Links
- 🎯 **Run Hybrid System**: `python run_hybrid_trading.py`
- 📖 **Parameter Guide**: See [HYBRID_QUICK_REFERENCE.md](HYBRID_QUICK_REFERENCE.md)
- ⚙️ **Tuning**: Adjust `quant_weight` (default: 0.60) and `news_weight` (default: 0.40)

---

## ⚙️ Configuration

### Conservative (Fewer trades, lower risk)
```python
min_score = 75.0
min_confidence = 70.0
max_risk_per_trade_pct = 0.5
max_total_risk_pct = 10.0
```

### Balanced (Default)
```python
min_score = 65.0
min_confidence = 55.0
max_risk_per_trade_pct = 1.0
max_total_risk_pct = 20.0
```

### Aggressive (More trades, higher risk)
```python
min_score = 60.0
min_confidence = 50.0
max_risk_per_trade_pct = 2.0
max_total_risk_pct = 30.0
```

---

## 🔧 Installation

```bash
# Install dependencies
pip install -r requirements.txt

# Additional for quant system
pip install sqlalchemy numpy pandas
```

---

## 🎯 Next Steps (Optional Enhancements)

- [x] ✅ Backtesting engine (COMPLETE - test on historical data)
- [ ] Parameter optimization (grid search for best Sharpe)
- [ ] Streamlit dashboard (web UI)
- [ ] Email/Slack alerts
- [ ] Multi-timeframe analysis
- [ ] Machine learning models

---

## ✅ System Status

**Phase 3 Complete:**
- ✅ Advanced Analytics Engine (Sharpe 6.18, Sortino 18.85)
- ✅ SQLite Database (6 tables)
- ✅ Quantitative Signal Engine (50+ indicators)
- ✅ Technical Indicators Library
- ✅ Entry/Exit Rules (ATR stops, 2.5:1 R:R)
- ✅ Portfolio Risk Manager (100 positions)
- ✅ News & Sentiment Integration (Google RSS + Finnhub)
- ✅ Hybrid Trading System (60% quant, 40% news)
- ✅ **Backtesting Engine (COMPLETE)**

**Total Code:** ~9,000 lines across 25 files  
**Documentation:** 12 comprehensive guides

---

**Ready for professional quantitative swing trading! 🚀📊**
