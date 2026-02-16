"""
Advanced Trading Analytics

Professional-grade metrics:
- Sharpe & Sortino ratios
- Drawdown analysis
- Return metrics (total, annualized, monthly)
- Trade statistics
- Risk metrics
"""

import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Any
from dataclasses import dataclass


@dataclass
class PerformanceMetrics:
    """Complete performance metrics snapshot."""
    
    # Return metrics
    total_return_pct: float
    annualized_return_pct: float
    monthly_returns: List[float]
    cumulative_pnl: float
    
    # Risk metrics
    sharpe_ratio: float
    sortino_ratio: float
    calmar_ratio: float
    max_drawdown_pct: float
    drawdown_duration_days: int
    volatility_daily_pct: float
    
    # Trade metrics
    win_rate_pct: float
    profit_factor: float
    avg_win: float
    avg_loss: float
    best_trade: float
    worst_trade: float
    consecutive_wins: int
    consecutive_losses: int
    recovery_factor: float
    
    # Information
    num_trades: int
    num_wins: int
    num_losses: int
    calculation_date: str


class AdvancedAnalytics:
    """Calculate advanced trading metrics."""
    
    # Standard risk-free rate (annual)
    RISK_FREE_RATE = 0.05
    TRADING_DAYS = 252
    
    def __init__(self, risk_free_rate: float = 0.05):
        self.risk_free_rate = risk_free_rate
    
    def calculate_sharpe_ratio(
        self,
        returns: List[float],
        periods_per_year: int = 252,
    ) -> float:
        """
        Calculate Sharpe Ratio.
        
        Sharpe = (avg_return - risk_free_rate) / std_dev
        
        Interpretation:
        - > 1.0: Good
        - > 1.5: Very good
        - > 2.0: Excellent
        """
        if len(returns) < 2:
            return 0.0
        
        returns_array = np.array(returns)
        excess_returns = returns_array - (self.risk_free_rate / periods_per_year)
        
        if np.std(excess_returns) == 0:
            return 0.0
        
        sharpe = np.mean(excess_returns) / np.std(excess_returns)
        sharpe_annualized = sharpe * np.sqrt(periods_per_year)
        
        return round(sharpe_annualized, 2)
    
    def calculate_sortino_ratio(
        self,
        returns: List[float],
        periods_per_year: int = 252,
    ) -> float:
        """
        Calculate Sortino Ratio.
        
        Like Sharpe, but only penalizes downside volatility.
        Sortino = (avg_return - risk_free_rate) / downside_std_dev
        
        Better for strategies with asymmetric returns.
        """
        if len(returns) < 2:
            return 0.0
        
        returns_array = np.array(returns)
        excess_returns = returns_array - (self.risk_free_rate / periods_per_year)
        
        # Downside deviation (only negative returns)
        downside_returns = returns_array[returns_array < 0]
        if len(downside_returns) == 0:
            # No losing periods - infinite sortino
            return float('inf') if np.mean(excess_returns) > 0 else 0.0
        
        downside_std = np.std(downside_returns)
        
        if downside_std == 0:
            return 0.0
        
        sortino = np.mean(excess_returns) / downside_std
        sortino_annualized = sortino * np.sqrt(periods_per_year)
        
        return round(sortino_annualized, 2)
    
    def calculate_max_drawdown(
        self,
        equity_curve: List[float],
    ) -> Tuple[float, int]:
        """
        Calculate maximum drawdown and its duration.
        
        Returns:
            (max_drawdown_pct, duration_days)
        
        Interpretation:
        - Largest peak-to-trough decline
        - How long it took to recover
        """
        if len(equity_curve) < 2:
            return 0.0, 0
        
        equity = np.array(equity_curve)
        peak = np.maximum.accumulate(equity)
        drawdown = (equity - peak) / peak
        max_dd = np.min(drawdown)
        max_dd_pct = abs(max_dd * 100.0)
        
        # Find duration: from peak to recovery
        max_dd_idx = np.argmin(drawdown)
        
        # Find when peak occurred before this trough
        peak_idx = np.argmax(equity[:max_dd_idx + 1])
        
        # Find when equity recovered to previous peak
        recovery_idx = max_dd_idx
        for i in range(max_dd_idx + 1, len(equity)):
            if equity[i] >= equity[peak_idx]:
                recovery_idx = i
                break
        
        duration = recovery_idx - peak_idx
        
        return round(max_dd_pct, 2), duration
    
    def calculate_calmar_ratio(
        self,
        annual_return_pct: float,
        max_drawdown_pct: float,
    ) -> float:
        """
        Calculate Calmar Ratio.
        
        Calmar = annual_return / max_drawdown
        
        Higher is better (good return with manageable drawdown).
        """
        if max_drawdown_pct == 0:
            return float('inf') if annual_return_pct > 0 else 0.0
        
        calmar = annual_return_pct / max_drawdown_pct
        return round(calmar, 2)
    
    def calculate_profit_factor(
        self,
        trades: List[Dict[str, float]],
    ) -> float:
        """
        Calculate Profit Factor.
        
        Profit Factor = Gross Profit / Gross Loss
        
        Interpretation:
        - > 1.5: Profitable system
        - > 2.0: Excellent
        - > 3.0: Outstanding
        """
        winning_trades = [t['pnl'] for t in trades if t.get('pnl', 0) > 0]
        losing_trades = [t['pnl'] for t in trades if t.get('pnl', 0) < 0]
        
        gross_profit = sum(winning_trades) if winning_trades else 0.0
        gross_loss = abs(sum(losing_trades)) if losing_trades else 0.0
        
        if gross_loss == 0:
            return float('inf') if gross_profit > 0 else 0.0
        
        profit_factor = gross_profit / gross_loss
        return round(profit_factor, 2)
    
    def calculate_recovery_factor(
        self,
        total_pnl: float,
        max_drawdown_pct: float,
        max_equity: float,
    ) -> float:
        """
        Calculate Recovery Factor.
        
        Recovery = Total Profit / Max Drawdown (in dollars)
        
        How quickly profits exceed largest drawdown.
        Higher is better.
        """
        max_dd_amount = (max_drawdown_pct / 100.0) * max_equity
        
        if max_dd_amount == 0:
            return float('inf') if total_pnl > 0 else 0.0
        
        recovery = total_pnl / max_dd_amount
        return round(recovery, 2)
    
    def calculate_win_streaks(self, trades: List[Dict[str, float]]) -> Tuple[int, int]:
        """
        Calculate longest consecutive wins and losses.
        
        Returns:
            (max_consecutive_wins, max_consecutive_losses)
        """
        if not trades:
            return 0, 0
        
        results = [1 if t.get('pnl', 0) > 0 else 0 for t in trades]
        
        max_wins = 0
        max_losses = 0
        current_wins = 0
        current_losses = 0
        
        for result in results:
            if result == 1:
                current_wins += 1
                current_losses = 0
                max_wins = max(max_wins, current_wins)
            else:
                current_losses += 1
                current_wins = 0
                max_losses = max(max_losses, current_losses)
        
        return max_wins, max_losses
    
    def calculate_monthly_returns(
        self,
        trades: List[Dict[str, Any]],
    ) -> Dict[str, float]:
        """
        Calculate returns by month.
        
        Returns:
            {month: pnl, ...}
        """
        monthly = {}
        
        for trade in trades:
            if trade.get('status') != 'CLOSED':
                continue
            
            entry_time = trade.get('entry_timestamp', '')
            if not entry_time:
                continue
            
            # Extract month (YYYY-MM)
            month = entry_time[:7]
            pnl = trade.get('pnl', 0)
            
            if month not in monthly:
                monthly[month] = 0.0
            
            monthly[month] += pnl
        
        return {month: round(pnl, 2) for month, pnl in monthly.items()}
    
    def calculate_equity_curve(
        self,
        trades: List[Dict[str, Any]],
        starting_equity: float = 100000.0,
    ) -> List[float]:
        """
        Calculate equity curve from trades.
        
        Returns:
            [equity_value, ...] over time
        """
        equity = [starting_equity]
        current = starting_equity
        
        # Sort trades by entry time
        sorted_trades = sorted(
            trades,
            key=lambda t: t.get('entry_timestamp', ''),
        )
        
        for trade in sorted_trades:
            if trade.get('status') == 'CLOSED':
                pnl = trade.get('pnl', 0)
                current += pnl
                equity.append(current)
        
        return equity
    
    def calculate_all_metrics(
        self,
        trades: List[Dict[str, Any]],
        starting_equity: float = 100000.0,
        periods_per_year: int = 252,
    ) -> PerformanceMetrics:
        """
        Calculate all performance metrics in one call.
        """
        
        # Filter closed trades
        closed_trades = [t for t in trades if t.get('status') == 'CLOSED']
        
        if not closed_trades:
            return PerformanceMetrics(
                total_return_pct=0.0,
                annualized_return_pct=0.0,
                monthly_returns=[],
                cumulative_pnl=0.0,
                sharpe_ratio=0.0,
                sortino_ratio=0.0,
                calmar_ratio=0.0,
                max_drawdown_pct=0.0,
                drawdown_duration_days=0,
                volatility_daily_pct=0.0,
                win_rate_pct=0.0,
                profit_factor=0.0,
                avg_win=0.0,
                avg_loss=0.0,
                best_trade=0.0,
                worst_trade=0.0,
                consecutive_wins=0,
                consecutive_losses=0,
                recovery_factor=0.0,
                num_trades=0,
                num_wins=0,
                num_losses=0,
                calculation_date=datetime.utcnow().isoformat(),
            )
        
        # Basic P&L
        total_pnl = sum(t.get('pnl', 0) for t in closed_trades)
        total_return_pct = (total_pnl / starting_equity) * 100.0
        
        # Equity curve
        equity_curve = self.calculate_equity_curve(closed_trades, starting_equity)
        
        # Calculate trading days elapsed
        first_trade = closed_trades[0].get('entry_timestamp', '')
        last_trade = closed_trades[-1].get('exit_timestamp', '')
        
        trading_days = 252  # Default to 1 year
        if first_trade and last_trade:
            try:
                start_dt = datetime.fromisoformat(first_trade)
                end_dt = datetime.fromisoformat(last_trade)
                trading_days = max(1, (end_dt - start_dt).days)
                if trading_days == 0:
                    trading_days = 1
            except:
                pass
        
        # Annualized return
        years_active = trading_days / 365.0
        if years_active > 0:
            annualized_return = ((1 + total_return_pct/100.0) ** (1.0/years_active)) - 1.0
            annualized_return_pct = annualized_return * 100.0
        else:
            annualized_return_pct = total_return_pct
        
        # Daily returns (for ratio calculations)
        daily_returns_pct = np.diff(equity_curve) / equity_curve[:-1] * 100.0
        daily_returns = daily_returns_pct / 100.0
        
        # Drawdown
        max_dd_pct, dd_duration = self.calculate_max_drawdown(equity_curve)
        
        # Volatility
        volatility_daily_pct = np.std(daily_returns_pct) if len(daily_returns_pct) > 0 else 0.0
        
        # Ratios
        sharpe = self.calculate_sharpe_ratio(daily_returns, periods_per_year)
        sortino = self.calculate_sortino_ratio(daily_returns, periods_per_year)
        calmar = self.calculate_calmar_ratio(annualized_return_pct, max_dd_pct)
        
        # Trade statistics
        win_trades = [t for t in closed_trades if t.get('pnl', 0) > 0]
        loss_trades = [t for t in closed_trades if t.get('pnl', 0) < 0]
        
        win_rate_pct = (len(win_trades) / len(closed_trades) * 100.0) if closed_trades else 0.0
        avg_win = np.mean([t.get('pnl', 0) for t in win_trades]) if win_trades else 0.0
        avg_loss = np.mean([t.get('pnl', 0) for t in loss_trades]) if loss_trades else 0.0
        best_trade = max([t.get('pnl', 0) for t in closed_trades]) if closed_trades else 0.0
        worst_trade = min([t.get('pnl', 0) for t in closed_trades]) if closed_trades else 0.0
        
        # Other metrics
        profit_factor = self.calculate_profit_factor(closed_trades)
        max_wins, max_losses = self.calculate_win_streaks(closed_trades)
        recovery_factor = self.calculate_recovery_factor(
            total_pnl,
            max_dd_pct,
            equity_curve[0]
        )
        
        # Monthly returns
        monthly = self.calculate_monthly_returns(closed_trades)
        monthly_returns = list(monthly.values())
        
        return PerformanceMetrics(
            total_return_pct=round(total_return_pct, 2),
            annualized_return_pct=round(annualized_return_pct, 2),
            monthly_returns=monthly_returns,
            cumulative_pnl=round(total_pnl, 2),
            sharpe_ratio=sharpe,
            sortino_ratio=sortino,
            calmar_ratio=calmar,
            max_drawdown_pct=max_dd_pct,
            drawdown_duration_days=dd_duration,
            volatility_daily_pct=round(volatility_daily_pct, 2),
            win_rate_pct=round(win_rate_pct, 2),
            profit_factor=profit_factor,
            avg_win=round(avg_win, 2),
            avg_loss=round(avg_loss, 2),
            best_trade=round(best_trade, 2),
            worst_trade=round(worst_trade, 2),
            consecutive_wins=max_wins,
            consecutive_losses=max_losses,
            recovery_factor=recovery_factor,
            num_trades=len(closed_trades),
            num_wins=len(win_trades),
            num_losses=len(loss_trades),
            calculation_date=datetime.utcnow().isoformat(),
        )
    
    def display_metrics(self, metrics: PerformanceMetrics):
        """Pretty-print performance metrics."""
        print(f"\n{'='*70}")
        print(f"PERFORMANCE METRICS (as of {metrics.calculation_date[:10]})")
        print(f"{'='*70}\n")
        
        print("RETURNS")
        print(f"  Total Return:        {metrics.total_return_pct:>10.2f}%")
        print(f"  Annualized Return:   {metrics.annualized_return_pct:>10.2f}%")
        print(f"  Cumulative P&L:      ${metrics.cumulative_pnl:>10,.2f}\n")
        
        print("RISK METRICS")
        print(f"  Sharpe Ratio:        {metrics.sharpe_ratio:>10.2f}")
        print(f"  Sortino Ratio:       {metrics.sortino_ratio:>10.2f}")
        print(f"  Calmar Ratio:        {metrics.calmar_ratio:>10.2f}")
        print(f"  Max Drawdown:        {metrics.max_drawdown_pct:>10.2f}%")
        print(f"  Drawdown Duration:   {metrics.drawdown_duration_days:>10} days")
        print(f"  Daily Volatility:    {metrics.volatility_daily_pct:>10.2f}%\n")
        
        print("TRADE STATISTICS")
        print(f"  Total Trades:        {metrics.num_trades:>10}")
        print(f"  Wins:                {metrics.num_wins:>10}")
        print(f"  Losses:              {metrics.num_losses:>10}")
        print(f"  Win Rate:            {metrics.win_rate_pct:>10.2f}%")
        print(f"  Profit Factor:       {metrics.profit_factor:>10.2f}")
        print(f"  Recovery Factor:     {metrics.recovery_factor:>10.2f}\n")
        
        print("TRADE DETAILS")
        print(f"  Avg Win:             ${metrics.avg_win:>10,.2f}")
        print(f"  Avg Loss:            ${metrics.avg_loss:>10,.2f}")
        print(f"  Best Trade:          ${metrics.best_trade:>10,.2f}")
        print(f"  Worst Trade:         ${metrics.worst_trade:>10,.2f}")
        print(f"  Best Win Streak:     {metrics.consecutive_wins:>10}")
        print(f"  Worst Loss Streak:   {metrics.consecutive_losses:>10}")
        print(f"{'='*70}\n")
