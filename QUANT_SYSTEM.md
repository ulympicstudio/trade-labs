# Quantitative Trading System Documentation

## 🎯 Overview

The **Quantitative Data Arm** is a sophisticated trading engine designed for **high-frequency swing trading** across hundreds of symbols simultaneously. It calculates hundreds of technical and statistical metrics per symbol to generate high-probability trade signals.

## 🏗️ Architecture

```
src/quant/
├── technical_indicators.py    # 50+ technical calculations
├── quant_scorer.py            # Probability-based scoring engine
├── quant_scanner.py           # Market scanner with IB integration
├── portfolio_risk_manager.py  # Portfolio-level risk controls
└── __init__.py
```

## 📊 Technical Indicators Engine

### Calculated Metrics (50+)

**Momentum Indicators:**
- RSI (7, 14, 21 periods)
- MACD (12, 26, 9)
- Stochastic Oscillator
- Williams %R

**Volatility Indicators:**
- ATR (14, 21 periods)
- Bollinger Bands (upper, middle, lower, width, position)
- 20-day volatility

**Trend Indicators:**
- EMA (9, 21, 50)
- SMA (20, 50, 200)

**Volume Indicators:**
- Volume ratio vs 20-day average
- Volume spike detection
- Chaikin Money Flow

**Mean Reversion:**
- Z-scores (20, 50-day)
- Price vs SMA distance
- 52-week high/low

**Performance:**
- 5-day, 10-day returns
- Daily volatility

**Microstructure:**
- Bid-ask spread percentage

### Usage

```python
from src.quant import TechnicalIndicators

tech = TechnicalIndicators(lookback=252)

indicators = tech.calculate_all_indicators(
    symbol="AAPL",
    timestamp="2026-02-14T12:00:00",
    highs=[...],    # Historical highs
    lows=[...],     # Historical lows
    closes=[...],   # Historical closes
    volumes=[...],  # Historical volumes
    bid=150.25,
    ask=150.27
)

print(f"RSI: {indicators.rsi_14}")
print(f"ATR: ${indicators.atr_14}")
print(f"Volatility: {indicators.volatility_20d}%")
```

## 🎲 Quantitative Scoring Engine

### 5-Component Probability Model

Combines multiple signals into a composite probability score (0-100):

| Component | Weight | Purpose |
|-----------|--------|---------|
| **Momentum** | 30% | Trend-following strength |
| **Mean Reversion** | 25% | Oversold/overbought bounce potential |
| **Volatility** | 20% | Risk-adjusted opportunity |
| **Volume** | 15% | Liquidity and conviction |
| **Microstructure** | 10% | Execution quality |

### Scoring Logic

**Momentum (0-100):**
- RSI in bullish/bearish zones
- MACD crossovers
- EMA alignment (9 > 21 > 50)
- Recent price action strength

**Mean Reversion (0-100):**
- RSI < 30 (oversold) or > 70 (overbought)
- Bollinger Band extremes
- Z-score > 2σ from mean
- Stochastic extremes

**Volatility (0-100):**
- High ATR relative to price (>3%)
- Wide Bollinger Bands (>5%)
- Optimal volatility range (20-60% annualized)

**Volume (0-100):**
- Volume > 1.5x average
- Recent volume spikes
- Strong CMF (buying/selling pressure)

**Microstructure (0-100):**
- Tight bid-ask spreads (<0.1% = perfect)

### Trade Recommendations

For each opportunity, generates:

- **Direction**: LONG or SHORT
- **Entry Price**: Limit order suggestion
- **Stop Loss**: Based on ATR (1.5-2 ATRs)
- **Profit Target**: Risk:Reward 2.5:1
- **Expected Return %**
- **Confidence Level (0-100)**

### Usage

```python
from src.quant import QuantScorer

scorer = QuantScorer()

score = scorer.calculate_score(indicators, current_price=150.50)

print(f"Total Score: {score.total_score}/100")
print(f"Direction: {score.direction}")
print(f"Entry: ${score.suggested_entry}")
print(f"Stop: ${score.suggested_stop}")
print(f"Target: ${score.suggested_target}")
print(f"R:R: {score.risk_reward_ratio}:1")
print(f"Key Signals: {score.key_signals}")
```

## 🔍 Quant Market Scanner

### Full Pipeline

1. **Scan Market**: Get most active stocks from IB
2. **Quality Filter**: Price, spread, volume checks
3. **Fetch Historical Data**: 1 year OHLCV bars
4. **Calculate Indicators**: 50+ metrics per symbol
5. **Generate Scores**: Probability-based ranking
6. **Rank Opportunities**: Sort by total score

### Usage

```python
from ib_insync import IB
from src.quant import run_quant_scan

ib = IB()
ib.connect("127.0.0.1", 7497, clientId=1)

# Run comprehensive scan
scores = run_quant_scan(
    ib,
    candidate_limit=100,      # Scan top 100 active
    min_score=60.0,           # Minimum score threshold
    min_confidence=50.0,      # Minimum confidence
    display_top_n=20          # Display top 20
)

# Top opportunity
best = scores[0]
print(f"{best.symbol}: Score {best.total_score}, "
      f"Entry ${best.suggested_entry}, "
      f"Target ${best.suggested_target}")
```

### Output Format

```
TOP 20 QUANTITATIVE SWING TRADE OPPORTUNITIES
================================================================================
Rank  Symbol  Score   Conf   Dir    Entry      Stop       Target     R:R   Exp%
1     AAPL    87.3    92.1   LONG   $150.25    $145.80    $161.38    2.50  +7.4%
2     MSFT    85.2    89.3   LONG   $405.50    $398.20    $423.75    2.50  +4.5%
3     TSLA    82.7    88.1   SHORT  $185.30    $189.50    $174.80    2.50  -5.7%
...
```

## 🎯 Portfolio Risk Manager

### Portfolio-Level Risk Controls

Manages 100+ simultaneous positions with sophisticated safeguards:

**Capital Limits:**
- Max positions: 100 (configurable)
- Max capital utilization: 90%
- Max position size: 5% per symbol

**Risk Limits:**
- Max risk per trade: 1% of capital
- Max total risk: 20% of capital
- Position-level stop losses

**Diversification:**
- No duplicate symbols
- Sector exposure limits
- Correlation management (planned)

### Position Sizing Algorithm

Uses **fixed fractional risk** approach:

```python
# Calculate position size based on stop loss distance
risk_per_share = abs(entry_price - stop_loss)
max_risk_dollars = total_capital * (max_risk_pct / 100)
quantity = floor(max_risk_dollars / risk_per_share)

# Apply position size cap
max_position_size = total_capital * (max_position_size_pct / 100)
if quantity * entry_price > max_position_size:
    quantity = floor(max_position_size / entry_price)
```

### Usage

```python
from src.quant import PortfolioRiskManager

portfolio = PortfolioRiskManager(
    total_capital=100000,
    max_positions=100,
    max_risk_per_trade_pct=1.0,
    max_total_risk_pct=20.0,
    max_position_size_pct=5.0
)

# Evaluate opportunities
approved_positions = portfolio.prioritize_opportunities(scores)

# Each approved position includes:
for position in approved_positions:
    print(f"{position['symbol']}: "
          f"{position['quantity']} shares, "
          f"${position['position_size']:,.2f}, "
          f"risk {position['risk_pct']:.2f}%")

# Monitor portfolio
portfolio.display_portfolio_status()
portfolio.display_open_positions()
```

### Portfolio Metrics

Real-time monitoring:

```python
metrics = portfolio.get_portfolio_metrics()

print(f"Deployed Capital: ${metrics.deployed_capital:,.2f} ({metrics.capital_utilization_pct:.1f}%)")
print(f"Open Positions: {metrics.num_positions} ({metrics.num_long} long, {metrics.num_short} short)")
print(f"Total Risk: ${metrics.total_risk_amount:,.2f} ({metrics.total_risk_pct:.1f}%)")
print(f"Unrealized P&L: ${metrics.unrealized_pnl:+,.2f} ({metrics.unrealized_pnl_pct:+.2f}%)")
```

## 🚀 Complete Trading Workflow

### Step 1: Scan Market

```python
from ib_insync import IB
from src.quant import QuantMarketScanner

ib = IB()
ib.connect("127.0.0.1", 7497, clientId=1)

scanner = QuantMarketScanner(ib)

# Scan and score
scores = scanner.scan_and_score(
    candidate_limit=100,
    min_score=65.0,
    min_confidence=55.0
)

print(f"Found {len(scores)} opportunities")
scanner.display_top_opportunities(scores, top_n=20)
```

### Step 2: Portfolio Filtering

```python
from src.quant import PortfolioRiskManager

portfolio = PortfolioRiskManager(
    total_capital=100000,
    max_positions=50,
    max_risk_per_trade_pct=1.0
)

# Filter through risk controls
approved = portfolio.prioritize_opportunities(scores)

print(f"Approved {len(approved)} positions")
```

### Step 3: Order Execution

```python
from src.execution.orders import place_limit_order

for position in approved:
    # Place limit buy/sell order
    order = place_limit_order(
        ib=ib,
        symbol=position['symbol'],
        action="BUY" if position['direction'] == "LONG" else "SELL",
        quantity=position['quantity'],
        limit_price=position['entry_price']
    )
    
    # Place stop loss
    stop_order = place_stop_order(
        ib=ib,
        symbol=position['symbol'],
        action="SELL" if position['direction'] == "LONG" else "BUY",
        quantity=position['quantity'],
        stop_price=position['stop_loss']
    )
    
    # Place profit target
    target_order = place_limit_order(
        ib=ib,
        symbol=position['symbol'],
        action="SELL" if position['direction'] == "LONG" else "BUY",
        quantity=position['quantity'],
        limit_price=position['profit_target']
    )
```

### Step 4: Monitor Positions

```python
# Update prices regularly
for position in portfolio.positions:
    current_price = get_current_price(ib, position.symbol)
    portfolio.update_position_price(position.symbol, current_price)

# Display status
portfolio.display_portfolio_status()
portfolio.display_open_positions(top_n=20)
```

## 📈 Backtesting (Planned)

Future Phase 3 Task: Test strategies on historical data

```python
# Planned API
from src.backtest import BacktestEngine

engine = BacktestEngine(
    start_date="2024-01-01",
    end_date="2025-12-31",
    initial_capital=100000
)

results = engine.run_backtest(
    scanner=scanner,
    portfolio=portfolio
)

print(f"Total Return: {results.total_return_pct}%")
print(f"Sharpe Ratio: {results.sharpe_ratio}")
print(f"Max Drawdown: {results.max_drawdown}%")
```

## 🎛️ Configuration

### Scoring Weights

Customize component weights:

```python
scorer = QuantScorer(
    momentum_weight=0.35,       # More momentum bias
    mean_reversion_weight=0.20,
    volatility_weight=0.20,
    volume_weight=0.15,
    microstructure_weight=0.10
)
```

### Risk Parameters

Adjust risk tolerance:

```python
portfolio = PortfolioRiskManager(
    total_capital=100000,
    max_positions=100,
    max_risk_per_trade_pct=2.0,    # More aggressive
    max_total_risk_pct=30.0,       # Higher total risk
    max_position_size_pct=10.0,    # Larger positions
    max_capital_utilization_pct=95.0
)
```

## 🧪 Testing

Run comprehensive system test:

```bash
python test_quant_system.py
```

This tests:
- ✅ 50+ technical indicators
- ✅ 5-component scoring
- ✅ Portfolio risk management (100 positions)
- ✅ Multi-symbol scanning
- ✅ Position sizing and P&L tracking

## 🎯 Performance Characteristics

**Calculations per Symbol:**
- 50+ technical indicators
- 5-component probability score
- Entry/stop/target suggestions
- Risk metrics

**Processing Speed:**
- ~0.5-1 second per symbol (with IB data fetch)
- Can scan 100 symbols in ~60-90 seconds

**Portfolio Capacity:**
- Up to 100 simultaneous positions
- Real-time P&L tracking
- Portfolio-level risk monitoring

## 📚 Key Concepts

### Swing Trading

- Holding period: 2-15 days
- Targets: 2-10% moves
- Uses limit orders + trailing stops
- Multiple positions simultaneously

### Position Sizing

- Fixed fractional risk (1% per trade)
- Stop loss based on ATR (volatility)
- 2.5:1 risk:reward minimum

### Portfolio Risk

- No more than 20% total capital at risk
- Maximum 90% capital deployed
- Position concentration limits (5% max)

### Signal Generation

- Probabilistic (not binary)
- Multiple confirmation signals
- Both momentum and mean reversion
- Volume and volatility confirmation

## 🔗 Integration

Works seamlessly with existing Trade Labs infrastructure:

- **Interactive Brokers**: Market data and execution
- **Database**: Records all trades and signals
- **Analytics**: Performance metrics calculation
- **Logging**: Full audit trail

## 🚀 Next Steps

1. **Backtesting**: Test on historical data
2. **Optimization**: Tune parameters for max Sharpe
3. **Dashboard**: Streamlit UI for monitoring
4. **Alerts**: Email/Slack notifications
5. **Multi-Timeframe**: Add intraday signals
6. **Machine Learning**: Train predictive models

---

**Ready to trade hundreds of symbols with quantitative precision!** 📊🚀
