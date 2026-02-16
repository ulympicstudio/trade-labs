"""
Backtesting Engine for Trade Labs
Simulates hybrid trading system on historical data.
"""

import logging
from datetime import datetime, timedelta
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import pandas as pd
import numpy as np
from ib_insync import IB

from .historical_data import HistoricalDataManager
from src.quant.quant_scanner import QuantMarketScanner
from src.data.news_scorer import NewsScorer
from src.data.quant_news_integrator import QuantNewsIntegrator
from src.quant.portfolio_risk_manager import PortfolioRiskManager

logger = logging.getLogger(__name__)


@dataclass
class BacktestTrade:
    """Single trade in backtest."""
    symbol: str
    entry_date: datetime
    entry_price: float
    shares: int
    stop_price: float
    target_price: float
    exit_date: Optional[datetime] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None  # 'stop', 'target', 'time', 'manual'
    pnl: float = 0.0
    pnl_pct: float = 0.0
    
    def __post_init__(self):
        """Calculate P&L if trade is closed."""
        if self.exit_price is not None:
            self.pnl = (self.exit_price - self.entry_price) * self.shares
            self.pnl_pct = ((self.exit_price / self.entry_price) - 1) * 100


@dataclass
class BacktestStats:
    """Backtest performance statistics."""
    total_trades: int = 0
    winning_trades: int = 0
    losing_trades: int = 0
    win_rate: float = 0.0
    
    total_pnl: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    largest_win: float = 0.0
    largest_loss: float = 0.0
    
    profit_factor: float = 0.0  # Gross profit / Gross loss
    expectancy: float = 0.0  # Average trade P&L
    
    max_drawdown: float = 0.0
    max_drawdown_pct: float = 0.0
    
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    
    total_return: float = 0.0
    total_return_pct: float = 0.0
    
    avg_bars_in_trade: float = 0.0
    max_bars_in_trade: int = 0
    
    start_date: Optional[datetime] = None
    end_date: Optional[datetime] = None
    days_traded: int = 0


@dataclass
class BacktestResult:
    """Complete backtest results."""
    stats: BacktestStats
    trades: List[BacktestTrade] = field(default_factory=list)
    equity_curve: pd.DataFrame = field(default_factory=pd.DataFrame)
    daily_returns: pd.Series = field(default_factory=pd.Series)
    
    def print_summary(self):
        """Print backtest summary."""
        s = self.stats
        
        print("\n" + "="*80)
        print("BACKTEST RESULTS")
        print("="*80)
        
        print(f"\nPeriod: {s.start_date.strftime('%Y-%m-%d')} to {s.end_date.strftime('%Y-%m-%d')} ({s.days_traded} days)")
        
        print(f"\n📊 PERFORMANCE:")
        print(f"  Total Return:    ${s.total_pnl:,.2f} ({s.total_return_pct:+.2f}%)")
        print(f"  Max Drawdown:    ${s.max_drawdown:,.2f} ({s.max_drawdown_pct:.2f}%)")
        print(f"  Sharpe Ratio:    {s.sharpe_ratio:.2f}")
        print(f"  Sortino Ratio:   {s.sortino_ratio:.2f}")
        
        print(f"\n📈 TRADES:")
        print(f"  Total Trades:    {s.total_trades}")
        print(f"  Winners:         {s.winning_trades} ({s.win_rate:.1f}%)")
        print(f"  Losers:          {s.losing_trades} ({100-s.win_rate:.1f}%)")
        
        print(f"\n💰 WIN/LOSS:")
        print(f"  Average Win:     ${s.avg_win:,.2f}")
        print(f"  Average Loss:    ${s.avg_loss:,.2f}")
        print(f"  Largest Win:     ${s.largest_win:,.2f}")
        print(f"  Largest Loss:    ${s.largest_loss:,.2f}")
        print(f"  Profit Factor:   {s.profit_factor:.2f}")
        print(f"  Expectancy:      ${s.expectancy:.2f}")
        
        print(f"\n⏱️  HOLDING PERIOD:")
        print(f"  Average:         {s.avg_bars_in_trade:.1f} days")
        print(f"  Maximum:         {s.max_bars_in_trade} days")
        
        print("\n" + "="*80 + "\n")


class BacktestEngine:
    """
    Backtesting engine for hybrid trading system.
    
    Features:
    - Historical data simulation
    - Position sizing and risk management
    - Stop loss and profit target execution
    - Performance metrics calculation
    - Equity curve generation
    """
    
    def __init__(self, ib: IB,
                 start_date: datetime,
                 end_date: datetime,
                 initial_capital: float = 100000.0,
                 quant_weight: float = 0.60,
                 news_weight: float = 0.40,
                 max_positions: int = 50,
                 max_holding_days: int = 30):
        """
        Initialize backtest engine.
        
        Args:
            ib: Connected IB instance
            start_date: Backtest start date
            end_date: Backtest end date
            initial_capital: Starting capital
            quant_weight: Weight for technical analysis
            news_weight: Weight for news sentiment
            max_positions: Maximum concurrent positions
            max_holding_days: Max days to hold a position
        """
        self.ib = ib
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.quant_weight = quant_weight
        self.news_weight = news_weight
        self.max_positions = max_positions
        self.max_holding_days = max_holding_days
        
        # Initialize components
        self.data_manager = HistoricalDataManager(ib=ib)
        
        # Will be initialized during backtest
        self.news_scorer = None
        self.integrator = None
        self.scanner = None
        self.portfolio_manager = None
        
        # Backtest state
        self.current_capital = initial_capital
        self.open_trades: List[BacktestTrade] = []
        self.closed_trades: List[BacktestTrade] = []
        self.equity_history: List[Tuple[datetime, float]] = []
        
        logger.info(f"BacktestEngine initialized: {start_date} to {end_date}")
        logger.info(f"Capital: ${initial_capital:,.0f}, Max positions: {max_positions}")
    
    def run_backtest(self,
                    universe: List[str],
                    scan_frequency_days: int = 1,
                    min_unified_score: float = 65.0,
                    min_confidence: float = 60.0) -> BacktestResult:
        """
        Run complete backtest.
        
        Args:
            universe: List of symbols to trade
            scan_frequency_days: Days between scans
            min_unified_score: Minimum score to enter trade
            min_confidence: Minimum confidence level
            
        Returns:
            BacktestResult with all trades and statistics
        """
        logger.info("="*80)
        logger.info(f"STARTING BACKTEST: {self.start_date} to {self.end_date}")
        logger.info(f"Universe: {len(universe)} symbols")
        logger.info(f"Scan frequency: Every {scan_frequency_days} day(s)")
        logger.info("="*80)
        
        # Initialize trading components
        self._initialize_components()
        
        # Fetch historical data for universe
        logger.info("\n📊 Fetching historical data...")
        historical_data = self.data_manager.get_multiple_symbols(
            symbols=universe,
            start_date=self.start_date - timedelta(days=100),  # Extra for indicators
            end_date=self.end_date
        )
        
        if not historical_data:
            logger.error("No historical data fetched")
            return BacktestResult(stats=BacktestStats())
        
        logger.info(f"✅ Loaded {len(historical_data)} symbols")
        
        # Run day-by-day simulation
        current_date = self.start_date
        scan_counter = 0
        
        while current_date <= self.end_date:
            logger.info(f"\n📅 {current_date.strftime('%Y-%m-%d')} | Capital: ${self.current_capital:,.0f} | Open: {len(self.open_trades)}")
            
            # Update open positions (check stops/targets)
            self._update_open_positions(current_date, historical_data)
            
            # Scan for new trades (if it's a scan day)
            if scan_counter % scan_frequency_days == 0 and len(self.open_trades) < self.max_positions:
                self._scan_and_enter_trades(
                    current_date=current_date,
                    historical_data=historical_data,
                    min_unified_score=min_unified_score,
                    min_confidence=min_confidence
                )
            
            # Record equity
            total_equity = self._calculate_total_equity(current_date, historical_data)
            self.equity_history.append((current_date, total_equity))
            
            # Next day
            current_date += timedelta(days=1)
            scan_counter += 1
        
        # Close all remaining positions
        logger.info("\n🏁 Closing all remaining positions...")
        for trade in self.open_trades[:]:
            self._close_trade(trade, self.end_date, trade.entry_price, "backtest_end", historical_data)
        
        # Calculate statistics
        logger.info("\n📊 Calculating statistics...")
        stats = self._calculate_statistics()
        
        # Build equity curve
        equity_df = pd.DataFrame(self.equity_history, columns=['date', 'equity'])
        equity_df.set_index('date', inplace=True)
        
        # Calculate daily returns
        daily_returns = equity_df['equity'].pct_change().dropna()
        
        result = BacktestResult(
            stats=stats,
            trades=self.closed_trades,
            equity_curve=equity_df,
            daily_returns=daily_returns
        )
        
        result.print_summary()
        
        return result
    
    def _initialize_components(self):
        """Initialize trading components."""
        import os
        
        api_key = os.environ.get('FINNHUB_API_KEY')
        
        self.news_scorer = NewsScorer(earnings_api_key=api_key)
        self.integrator = QuantNewsIntegrator(
            quant_weight=self.quant_weight,
            news_weight=self.news_weight,
            earnings_api_key=api_key
        )
        self.scanner = QuantMarketScanner(ib=self.ib)
        self.portfolio_manager = PortfolioRiskManager(
            total_capital=self.current_capital,
            max_positions=self.max_positions,
            max_risk_per_trade_pct=1.0,
            max_total_risk_pct=20.0
        )
    
    def _scan_and_enter_trades(self, current_date: datetime,
                               historical_data: Dict[str, pd.DataFrame],
                               min_unified_score: float,
                               min_confidence: float):
        """Scan for new trade opportunities and enter positions."""
        # Get symbols with data on current date
        available_symbols = []
        for symbol, df in historical_data.items():
            date_str = current_date.strftime('%Y-%m-%d')
            if any(df.index.strftime('%Y-%m-%d') == date_str):
                available_symbols.append(symbol)
        
        if not available_symbols:
            return
        
        # Score symbols (simplified for backtest - using historical prices only)
        opportunities = []
        
        for symbol in available_symbols[:20]:  # Limit to avoid slowdown
            # Get historical prices up to current date
            df = historical_data[symbol]
            hist = df[df.index <= current_date].tail(100)
            
            if len(hist) < 50:  # Need enough data for indicators
                continue
            
            # Simplified scoring (would need full news integration in production)
            # For backtest, focus on technical analysis
            try:
                # Get current price
                current_bar = hist.iloc[-1]
                entry_price = current_bar['close']
                
                # Simple technical score (RSI + trend)
                closes = hist['close'].values
                
                # RSI calculation
                deltas = np.diff(closes)
                gains = np.where(deltas > 0, deltas, 0)
                losses = np.where(deltas < 0, -deltas, 0)
                
                avg_gain = np.mean(gains[-14:]) if len(gains) >= 14 else 0
                avg_loss = np.mean(losses[-14:]) if len(losses) >= 14 else 0
                
                rs = avg_gain / avg_loss if avg_loss > 0 else 0
                rsi = 100 - (100 / (1 + rs)) if rs > 0 else 50
                
                # Trend (20 vs 50 SMA)
                sma20 = np.mean(closes[-20:]) if len(closes) >= 20 else entry_price
                sma50 = np.mean(closes[-50:]) if len(closes) >= 50 else entry_price
                
                # Simple scoring
                score = 50.0
                
                if 30 < rsi < 70:  # Not extreme
                    score += 10
                if sma20 > sma50:  # Uptrend
                    score += 20
                if entry_price > sma20:  # Above short MA
                    score += 10
                
                # Volume check
                if len(hist) >= 20:
                    avg_vol = hist['volume'].tail(20).mean()
                    if current_bar['volume'] > avg_vol * 1.2:
                        score += 10
                
                if score >= min_unified_score:
                    # Calculate stop and target
                    atr = self._calculate_atr(hist, period=14)
                    stop_price = entry_price - (2.0 * atr)
                    target_price = entry_price + (2.5 * 2.0 * atr)
                    
                    opportunities.append({
                        'symbol': symbol,
                        'score': score,
                        'confidence': min(score, 95.0),
                        'entry_price': entry_price,
                        'stop_price': stop_price,
                        'target_price': target_price,
                        'atr': atr
                    })
            
            except Exception as e:
                logger.warning(f"Error scoring {symbol}: {e}")
                continue
        
        # Sort by score
        opportunities.sort(key=lambda x: x['score'], reverse=True)
        
        # Enter trades up to max positions
        positions_to_add = min(
            len(opportunities),
            self.max_positions - len(self.open_trades),
            5  # Max 5 new positions per scan
        )
        
        for opp in opportunities[:positions_to_add]:
            # Calculate position size (1% risk)
            risk_per_share = opp['entry_price'] - opp['stop_price']
            if risk_per_share <= 0:
                continue
            
            risk_amount = self.current_capital * 0.01  # 1% risk
            shares = int(risk_amount / risk_per_share)
            
            if shares < 1:
                continue
            
            # Check if we have enough capital
            cost = shares * opp['entry_price']
            if cost > self.current_capital * 0.3:  # Max 30% per position
                shares = int((self.current_capital * 0.3) / opp['entry_price'])
            
            if shares < 1:
                continue
            
            # Enter trade
            trade = BacktestTrade(
                symbol=opp['symbol'],
                entry_date=current_date,
                entry_price=opp['entry_price'],
                shares=shares,
                stop_price=opp['stop_price'],
                target_price=opp['target_price']
            )
            
            self.open_trades.append(trade)
            self.current_capital -= (shares * opp['entry_price'])
            
            logger.info(f"  ✅ ENTER {opp['symbol']}: {shares} shares @ ${opp['entry_price']:.2f} (Score: {opp['score']:.1f})")
    
    def _update_open_positions(self, current_date: datetime,
                              historical_data: Dict[str, pd.DataFrame]):
        """Check stops, targets, and time exits for open positions."""
        for trade in self.open_trades[:]:  # Copy list since we're modifying
            symbol = trade.symbol
            
            if symbol not in historical_data:
                continue
            
            df = historical_data[symbol]
            date_str = current_date.strftime('%Y-%m-%d')
            matching = df[df.index.strftime('%Y-%m-%d') == date_str]
            
            if matching.empty:
                continue
            
            bar = matching.iloc[0]
            
            # Check stop (use low of day)
            if bar['low'] <= trade.stop_price:
                self._close_trade(trade, current_date, trade.stop_price, "stop", historical_data)
                logger.info(f"  🛑 STOP {symbol}: ${trade.stop_price:.2f} (P&L: ${trade.pnl:.2f})")
                continue
            
            # Check target (use high of day)
            if bar['high'] >= trade.target_price:
                self._close_trade(trade, current_date, trade.target_price, "target", historical_data)
                logger.info(f"  🎯 TARGET {symbol}: ${trade.target_price:.2f} (P&L: ${trade.pnl:.2f})")
                continue
            
            # Check time exit
            days_in_trade = (current_date - trade.entry_date).days
            if days_in_trade >= self.max_holding_days:
                self._close_trade(trade, current_date, bar['close'], "time", historical_data)
                logger.info(f"  ⏰ TIME {symbol}: ${bar['close']:.2f} (P&L: ${trade.pnl:.2f})")
                continue
    
    def _close_trade(self, trade: BacktestTrade, exit_date: datetime,
                    exit_price: float, reason: str,
                    historical_data: Dict[str, pd.DataFrame]):
        """Close a trade and update capital."""
        trade.exit_date = exit_date
        trade.exit_price = exit_price
        trade.exit_reason = reason
        trade.pnl = (exit_price - trade.entry_price) * trade.shares
        trade.pnl_pct = ((exit_price / trade.entry_price) - 1) * 100
        
        self.current_capital += (trade.shares * exit_price)
        self.open_trades.remove(trade)
        self.closed_trades.append(trade)
    
    def _calculate_total_equity(self, current_date: datetime,
                                historical_data: Dict[str, pd.DataFrame]) -> float:
        """Calculate total equity (cash + open positions)."""
        equity = self.current_capital
        
        for trade in self.open_trades:
            if trade.symbol not in historical_data:
                equity += (trade.shares * trade.entry_price)  # Use entry if no data
                continue
            
            df = historical_data[trade.symbol]
            date_str = current_date.strftime('%Y-%m-%d')
            matching = df[df.index.strftime('%Y-%m-%d') == date_str]
            
            if matching.empty:
                equity += (trade.shares * trade.entry_price)
            else:
                current_price = matching.iloc[0]['close']
                equity += (trade.shares * current_price)
        
        return equity
    
    def _calculate_statistics(self) -> BacktestStats:
        """Calculate comprehensive backtest statistics."""
        stats = BacktestStats()
        
        if not self.closed_trades:
            return stats
        
        stats.total_trades = len(self.closed_trades)
        
        # Win/Loss
        winners = [t for t in self.closed_trades if t.pnl > 0]
        losers = [t for t in self.closed_trades if t.pnl <= 0]
        
        stats.winning_trades = len(winners)
        stats.losing_trades = len(losers)
        stats.win_rate = (len(winners) / len(self.closed_trades)) * 100 if self.closed_trades else 0
        
        # P&L
        stats.total_pnl = sum(t.pnl for t in self.closed_trades)
        stats.avg_win = np.mean([t.pnl for t in winners]) if winners else 0
        stats.avg_loss = np.mean([t.pnl for t in losers]) if losers else 0
        stats.largest_win = max([t.pnl for t in winners]) if winners else 0
        stats.largest_loss = min([t.pnl for t in losers]) if losers else 0
        
        # Profit factor
        gross_profit = sum(t.pnl for t in winners) if winners else 0
        gross_loss = abs(sum(t.pnl for t in losers)) if losers else 0
        stats.profit_factor = gross_profit / gross_loss if gross_loss > 0 else 0
        
        # Expectancy
        stats.expectancy = stats.total_pnl / stats.total_trades if stats.total_trades > 0 else 0
        
        # Drawdown
        equity_curve = pd.Series([e for _, e in self.equity_history])
        cummax = equity_curve.cummax()
        drawdown = equity_curve - cummax
        stats.max_drawdown = abs(drawdown.min())
        stats.max_drawdown_pct = (drawdown / cummax * 100).min()
        
        # Returns
        stats.total_return = self.current_capital - self.initial_capital
        stats.total_return_pct = (self.current_capital / self.initial_capital - 1) * 100
        
        # Risk-adjusted metrics
        if len(equity_curve) > 1:
            returns = equity_curve.pct_change().dropna()
            
            if len(returns) > 0 and returns.std() > 0:
                stats.sharpe_ratio = (returns.mean() / returns.std()) * np.sqrt(252)
                
                negative_returns = returns[returns < 0]
                if len(negative_returns) > 0 and negative_returns.std() > 0:
                    stats.sortino_ratio = (returns.mean() / negative_returns.std()) * np.sqrt(252)
        
        # Holding period
        bars_in_trade = [(t.exit_date - t.entry_date).days for t in self.closed_trades if t.exit_date]
        stats.avg_bars_in_trade = np.mean(bars_in_trade) if bars_in_trade else 0
        stats.max_bars_in_trade = max(bars_in_trade) if bars_in_trade else 0
        
        # Dates
        stats.start_date = self.start_date
        stats.end_date = self.end_date
        stats.days_traded = (self.end_date - self.start_date).days
        
        return stats
    
    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> float:
        """Calculate Average True Range."""
        if len(df) < period + 1:
            return df['high'].iloc[-1] - df['low'].iloc[-1]
        
        high = df['high'].values
        low = df['low'].values
        close = df['close'].values
        
        tr = []
        for i in range(1, len(df)):
            hl = high[i] - low[i]
            hc = abs(high[i] - close[i-1])
            lc = abs(low[i] - close[i-1])
            tr.append(max(hl, hc, lc))
        
        atr = np.mean(tr[-period:]) if len(tr) >= period else np.mean(tr)
        return atr
